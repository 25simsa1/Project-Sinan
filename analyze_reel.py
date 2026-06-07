""" Instagram reel analyzer with gemini flash 2.5

For every saved reel HTML in ig_pages/, this will:
  - figure out the reel and get a video (embedded URL if still valid, else yt-dlp)
  - send it to Gemini for a structured JSON breakdown
  - cache results so a reel is never analyzed twice
  - track an estimated API spend

Settup: 
    pip3 install -U google-genai yt-dlp tqdm
    export GEMINI_API_KEY="your_key"

To run:
    python3 analyze_reel.py            # analyze new reels
    python3 analyze_reel.py --dry-run  # just report how many would run
"""

import os
import re
import sys
import json
import time
import glob
import subprocess

from google import genai
from tqdm import tqdm

HTML_DIR = "ig_pages"
CACHE_FILE = "analysis_cache.json"
MODEL = "gemini-2.5-flash"
TMP_VIDEO = "_tmp_reel.mp4"

# Rough cost estimate ONLY. Gemini pricing changes often and video is priced by
# how much of the clip is processed, check https://ai.google.dev/pricing and
# replace this with a real per-reel figure measured from your own usage.
EST_COST_PER_REEL = 0.01  # USD 

PROMPT = (
    "Analyze this Instagram reel and return ONLY valid JSON with these keys:\n"
    "{\n"
    '  "summary": "plain language summary",\n'
    '  "transcript": "every spoken word",\n'
    '  "on_screen_text": "any visible text as a string",\n'
    '  "visuals": "key objects, people, locations",\n'
    '  "purpose": "message or goal of the reel"\n'
    "}\n"
    "If a field has no content, use an empty string. "
    "Do not wrap the JSON in markdown fences."
)


# Cache helpers

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("  (cache file unreadable, starting fresh)")
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)



# Getting a shortcode + video from a saved HTML file

def shortcode_from_html(path, html):
    # Prefer the reel shortcode in the URL inside the page...
    m = re.search(r'instagram\.com/reels?/([A-Za-z0-9_-]+)', html)
    if m:
        return m.group(1)
    # ...otherwise fall back to the filename (e.g. DYrOxsxONyt.html)
    base = os.path.splitext(os.path.basename(path))[0]
    return base if re.fullmatch(r'[A-Za-z0-9_-]+', base) else None


def embedded_video_url(html):
    # Instagram embeds a video URL in the page JSON; often already expired.
    m = re.search(r'"video_url":"([^"]+)"', html)
    if m:
        return m.group(1).encode().decode("unicode_escape")
    return None


def get_video(path, html):
    """Return True if TMP_VIDEO now holds a real video, else False."""
    # Attempt 1: embedded URL (fast, but usually expired on old HTML)
    url = embedded_video_url(html)
    if url:
        try:
            subprocess.run(
                ["curl", "-sf", "-A", "Mozilla/5.0", "-o", TMP_VIDEO, url],
                check=True, timeout=60,
            )
            if is_real_video(TMP_VIDEO):
                print("  got video from embedded URL")
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    # Attempt 2: yt-dlp via the shortcode (reliable, fetches a fresh URL)
    code = shortcode_from_html(path, html)
    if not code:
        print("  could not find a shortcode")
        return False
    try:
        subprocess.run(
            ["yt-dlp", f"https://www.instagram.com/reel/{code}/",
             "-o", TMP_VIDEO, "--quiet", "--no-warnings"],
            check=True, timeout=300,
        )
        if is_real_video(TMP_VIDEO):
            print("  got video via yt-dlp")
            return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("  yt-dlp failed (rate limit or login needed)")
    return False


def is_real_video(path):
    # Reject HTML error pages saved as .mp4 (the bug from before).
    if not os.path.exists(path) or os.path.getsize(path) < 10_000:
        return False
    with open(path, "rb") as f:
        head = f.read(16)
    return b"ftyp" in head or head[:3] == b"\x00\x00\x00"


# Gemini 2.5 Flash caps video input length. Reels are short so this rarely
# matters, but if ever feeding longer clips, skip ones over the limit.
MAX_VIDEO_SECONDS = 3600  # ~1 hour


def video_too_long(path):
    """Return duration in seconds if over the limit, else None. Needs ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        seconds = float(out.stdout.strip())
        return seconds if seconds > MAX_VIDEO_SECONDS else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None  # ffprobe missing or unreadable — don't block, just proceed


# Gemini call with retry on transient 503s
def analyze_video(client, path):
    uploaded = client.files.upload(file=path)
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name == "FAILED":
        raise RuntimeError("Gemini could not process the video")

    last_err = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=[uploaded, PROMPT]
            )
            client.files.delete(name=uploaded.name)
            return parse_json(resp.text)
        except Exception as e:  # noqa: BLE001 — retry on transient server errors
            last_err = e
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                wait = 2 ** attempt
                print(f"  503, retrying in {wait}s...")
                time.sleep(wait)
            else:
                break
    client.files.delete(name=uploaded.name)
    raise last_err


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):  # strip accidental markdown fences
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_note": "model did not return valid JSON"}



# Main
def main():
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("Set GEMINI_API_KEY first.")

    cache = load_cache()
    html_files = sorted(glob.glob(os.path.join(HTML_DIR, "*.html")))
    if not html_files:
        sys.exit(f"No .html files found in {HTML_DIR}/")

    # Which reels are new? Map each html path to its shortcode once.
    def code_for(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            return shortcode_from_html(path, f.read()) or os.path.basename(path)

    pending = [p for p in html_files if code_for(p) not in cache]

    # B. Dry-run mode
    if "--dry-run" in sys.argv:
        print(f"{len(html_files)} reel(s) total, {len(cache)} already cached.")
        print(f"Would analyze {len(pending)} new reel(s).")
        sys.exit(0)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    print(f"Found {len(html_files)} reel(s). Cached: {len(cache)}. "
          f"New to do: {len(pending)}\n")
    analyzed_now = 0

    #  C. tqdm progress bar over the work list
    for path in tqdm(pending, desc="Processing reels", unit="reel"):
        with open(path, encoding="utf-8", errors="ignore") as f:
            html = f.read()
        code = shortcode_from_html(path, html) or os.path.basename(path)

        if not get_video(path, html):
            tqdm.write(f"[{code}] no video, skipping")
            continue

        # D. Skip clips that exceed Gemini's video length limit 
        too_long = video_too_long(TMP_VIDEO)
        if too_long:
            tqdm.write(f"[{code}] {too_long:.0f}s exceeds "
                       f"{MAX_VIDEO_SECONDS}s limit, skipping")
            os.remove(TMP_VIDEO)
            continue

        try:
            result = analyze_video(client, TMP_VIDEO)
        except Exception as e:  # noqa: BLE001
            tqdm.write(f"[{code}] analysis failed: {e}")
            continue
        finally:
            if os.path.exists(TMP_VIDEO):
                os.remove(TMP_VIDEO)

        # A.analysis + metadata 
        cache[code] = {
            "analysis": result,
            "analyzed_at": time.time(),
            "source_file": path,
        }
        save_cache(cache)          # save after each so progress is never lost
        analyzed_now += 1
        tqdm.write(f"[{code}] done: {result.get('summary', '')[:60]}")

    spent = analyzed_now * EST_COST_PER_REEL
    print("\n" + "-" * 50)
    print(f"Analyzed this run: {analyzed_now}")
    print(f"Total cached:      {len(cache)}")
    print(f"Estimated spend this run: ${spent:.2f} "
          f"(at ${EST_COST_PER_REEL}/reel — verify against real pricing)")
    print(f"Results saved in {CACHE_FILE}")


if __name__ == "__main__":
    main()