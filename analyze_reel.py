""" Instagram reel analyzer — Gemini Flash 2.5 OR a fully local backend.

For every saved reel HTML in ig_pages/, this will:
  - figure out the reel and get a video (embedded URL if still valid, else yt-dlp)
  - analyze it into structured JSON (summary, transcript, on_screen_text,
    visuals, purpose) using either Gemini or a local open-source stack
  - cache results so a reel is never analyzed twice
  - for the Gemini backend, track an estimated API spend

Setup

Shared:
    pip3 install -U yt-dlp tqdm

Gemini backend (default):
    pip3 install -U google-genai
    export GEMINI_API_KEY="your_key"

Local backend ( --backend local ), fully offline, no API quota:
    pip3 install -U faster-whisper ollama   # Whisper transcription + Ollama client
    brew install ffmpeg                     # macOS — frame sampling + duration
    ollama pull qwen2.5vl:7b                # the local vision-language model
    # Ollama usually auto-starts a server; if not, run:  ollama serve
    # (the script talks to its HTTP API at http://localhost:11434)

NOTE (Apple Silicon / MacBook Air): local video analysis is SLOW on this
hardware. Whisper runs on CPU (CTranslate2, no Metal) and a 7B vision model
under Ollama is heavy for a fanless laptop — expect tens of seconds to minutes
per reel. If performance is bad, try mlx-vlm instead of Ollama: it uses Apple's
MLX framework and runs vision models natively on the M-series GPU, which is
noticeably faster on Apple Silicon ( pip3 install -U mlx-vlm ; see
https://github.com/Blaizzy/mlx-vlm ). You'd swap analyze_video_local's Ollama
call for an mlx-vlm generate() call; everything else here stays the same.

To run:
    python3 analyze_reel.py                       # Gemini, analyze new reels
    python3 analyze_reel.py --backend local       # local stack, offline
    python3 analyze_reel.py --dry-run             # just report how many would run
    python3 analyze_reel.py --limit 5             # cap how many reels to do
    python3 analyze_reel.py --only DYrOxsxONyt    # one specific reel by shortcode
    python3 analyze_reel.py --backend local --frames 8 --ollama-model qwen2.5vl:7b
"""

import os
import re
import sys
import json
import time
import glob
import base64
import shutil
import argparse
import tempfile
import subprocess

from tqdm import tqdm

htmlDir = "ig_pages"
cacheFile = "analysis_cache.json"
model = "gemini-2.5-flash"
tmpVideo = "_tmp_reel.mp4"

# Playwright storage_state file written by ig_scraper.py / ig_collector.py.
cookieFile = "ig_cookies.json"

# Local backend defaults (all overridable via CLI).
ollamaHost = "http://localhost:11434"
defaultOllamaModel = "qwen2.5vl:7b"
defaultFrameCount = 6
whisperModel = "base"  # faster-whisper size: tiny/base/small/medium/large-v3
# Max width (px) frames are downscaled to before sending to the VL model.
# Full-res frames blow past the model's context window (each ~2.7k tokens);
# downscaling keeps the token count and memory use sane on a laptop.
frameMaxWidth = 768
# Ollama's default context window is only 4096 tokens, far too small for
# several images. Raise it so the frames + transcript + prompt fit.
ollamaNumCtx = 16384

# Rough cost estimate ONLY (Gemini backend). Gemini pricing changes often and
# video is priced by how much of the clip is processed, check
# https://ai.google.dev/pricing and replace this with a real per-reel figure
# measured from your own usage. The local backend costs $0.
estCostPerReel = 0.01  # USD

prompt = (
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

# Same five keys, but the local VL model is fed sampled frames + a Whisper
# transcript rather than the raw video. We already have an accurate transcript
# from Whisper, so the model is told to focus on the visuals and the meaning.
localPromptTemplate = (
    "You are analyzing an Instagram reel. You are given several evenly-spaced "
    "frames from the video and the audio transcript below.\n\n"
    "AUDIO TRANSCRIPT:\n{transcript}\n\n"
    "Return ONLY valid JSON with EXACTLY these keys:\n"
    "{{\n"
    '  "summary": "plain language summary of the whole reel",\n'
    '  "transcript": "the spoken words",\n'
    '  "on_screen_text": "any visible text in the frames, as a string",\n'
    '  "visuals": "key objects, people, locations seen in the frames",\n'
    '  "purpose": "message or goal of the reel"\n'
    "}}\n"
    "If a field has no content, use an empty string. "
    "Do not wrap the JSON in markdown fences."
)



# Cache helpers

def load_cache():
    if os.path.exists(cacheFile):
        try:
            with open(cacheFile) as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("  (cache file unreadable, starting fresh)")
    return {}


def save_cache(cache):
    with open(cacheFile, "w") as f:
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


def is_reel_shortcode(code):
    # Real Instagram shortcodes are exactly 11 chars of [A-Za-z0-9_-]. This
    # positive shape check rejects non-reel saved pages like reels.html
    # (-> "reels") while still excluding obvious placeholders such as an
    # all-repeated-character string (e.g. "XXXXXXXXXXX").
    if not code or not re.fullmatch(r'[A-Za-z0-9_-]{11}', code):
        return False
    if len(set(code)) == 1:  # all the same character -> placeholder
        return False
    return True


def embedded_video_url(html):
    # Instagram embeds a video URL in the page JSON; often already expired.
    m = re.search(r'"video_url":"([^"]+)"', html)
    if m:
        return m.group(1).encode().decode("unicode_escape")
    return None


# Cookie handling — convert Playwright JSON -> Netscape so yt-dlp accepts it.
#
# THE BUG THIS FIXES: yt-dlp's --cookies flag only understands the Netscape
# "cookies.txt" format. ig_cookies.json is a Playwright storage_state file
# (a JSON object with a "cookies" array). Passing the JSON straight to yt-dlp
# silently does nothing, so requests go out unauthenticated and Instagram
# replies "rate-limit reached or login required". We translate the JSON into a
# real Netscape file in a temp location and hand THAT to yt-dlp.


def playwright_json_to_netscape(json_path, out_path):
    """Convert a Playwright storage_state cookie JSON to a Netscape cookies.txt.

    Returns True on success, False if the file is missing/empty/unparseable.
    """
    if not os.path.exists(json_path):
        return False
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    # Playwright stores {"cookies": [...]}; tolerate a bare list too.
    cookies = data.get("cookies", []) if isinstance(data, dict) else data
    if not cookies:
        return False

    lines = ["# Netscape HTTP Cookie File",
             "# Generated by analyze_reel.py from " + json_path, ""]
    written = 0
    for c in cookies:
        name = c.get("name")
        value = c.get("value", "")
        domain = c.get("domain")
        if not name or not domain:
            continue
        path = c.get("path", "/")
        # Netscape: leading-dot domain means "include subdomains".
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        # expires == -1 (or missing) is a session cookie -> 0 means session.
        expires = c.get("expires", 0)
        try:
            expiry = int(expires) if expires and expires > 0 else 0
        except (TypeError, ValueError):
            expiry = 0
        # yt-dlp honours the #HttpOnly_ domain prefix for httpOnly cookies.
        domain_field = ("#HttpOnly_" + domain) if c.get("httpOnly") else domain
        lines.append("\t".join([domain_field, include_sub, path, secure,
                                str(expiry), name, value]))
        written += 1

    if not written:
        return False
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


def prepare_cookies():
    """Build a temp Netscape cookie file from cookieFile.

    Returns the path to the cookie file, or None if no usable cookies exist.
    Caller is responsible for deleting the returned path.
    """
    if not os.path.exists(cookieFile):
        print(f"  WARNING: {cookieFile} not found — yt-dlp will run without "
              f"auth and will likely hit Instagram's rate limit / login wall.\n"
              f"  Create it by running your scraper's login step "
              f"(e.g. python3 ig_collector.py --login).")
        return None
    fd, tmp = tempfile.mkstemp(prefix="yt_dlp_cookies_", suffix=".txt")
    os.close(fd)
    if playwright_json_to_netscape(cookieFile, tmp):
        print(f"  cookies: converted {cookieFile} -> Netscape format")
        return tmp
    os.remove(tmp)
    print(f"  WARNING: {cookieFile} had no usable cookies; running without "
          f"auth (expect rate-limit / login errors).")
    return None


def get_video(path, html, cookie_file=None):
    """Return True if tmpVideo now holds a real video, else False.

    Logs DOWNLOAD-stage failures distinctly so they can be told apart from
    analysis-stage failures upstream.
    """
    # Attempt 1: embedded URL (fast, but usually expired on old HTML)
    url = embedded_video_url(html)
    if url:
        try:
            subprocess.run(
                ["curl", "-sf", "-A", "Mozilla/5.0", "-o", tmpVideo, url],
                check=True, timeout=60,
            )
            if is_real_video(tmpVideo):
                print("  got video from embedded URL")
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass  # expired/blocked embedded URL — fall through to yt-dlp

    # Attempt 2: yt-dlp via the shortcode (reliable, fetches a fresh URL)
    code = shortcode_from_html(path, html)
    if not code:
        print("  DOWNLOAD FAILED: could not find a shortcode in the HTML")
        return False

    cmd = ["yt-dlp", f"https://www.instagram.com/reel/{code}/",
           "-o", tmpVideo, "--no-warnings",
           # Look more like a real browser; helps avoid the login wall.
           "--user-agent",
           ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36")]
    if cookie_file:
        cmd += ["--cookies", cookie_file]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("  DOWNLOAD FAILED: yt-dlp timed out after 300s")
        return False

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        lowered = stderr.lower()
        if "login required" in lowered or "rate-limit" in lowered \
                or "rate limit" in lowered or "429" in lowered:
            hint = ("Instagram returned a rate-limit / login wall. "
                    "Your cookies are missing, stale, or not being used — "
                    "refresh them with your scraper's --login step.")
        else:
            hint = stderr.splitlines()[-1] if stderr else "unknown yt-dlp error"
        print(f"  DOWNLOAD FAILED (yt-dlp): {hint}")
        return False

    if is_real_video(tmpVideo):
        print("  got video via yt-dlp")
        return True
    print("  DOWNLOAD FAILED: yt-dlp produced a non-video file "
          "(likely an HTML error page)")
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


def video_duration(path):
    """Return clip duration in seconds, or None if ffprobe is missing/fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ValueError, FileNotFoundError):
        return None  # ffprobe missing or unreadable


def video_too_long(path):
    """Return duration in seconds if over the Gemini limit, else None."""
    seconds = video_duration(path)
    if seconds is not None and seconds > MAX_VIDEO_SECONDS:
        return seconds
    return None


# Gemini backend

def analyze_video_gemini(client, path):
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
                model=model, contents=[uploaded, prompt]
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
    text = (text or "").strip()
    if text.startswith("```"):  # strip accidental markdown fences
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_note": "model did not return valid JSON"}


# Local backend — faster-whisper + ffmpeg frame sampling + Ollama VL model

_WHISPER = None  # lazily-loaded, reused across reels (loading is expensive)


def check_local_deps(ollama_model):
    """Fail fast with an exact remediation command if anything is missing."""
    # ffmpeg / ffprobe for frame sampling + duration.
    for tool, why in (("ffmpeg", "frame sampling"), ("ffprobe", "duration")):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"'{tool}' not found (needed for {why}). Install it with:\n"
                f"    brew install ffmpeg")

    # faster-whisper for transcription.
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "faster-whisper is not installed. Install it with:\n"
            "    pip3 install -U faster-whisper")

    # ollama python client.
    try:
        import ollama
    except ImportError:
        raise RuntimeError(
            "The Ollama Python client is not installed. Install it with:\n"
            "    pip3 install -U ollama")

    # Ollama server reachable + model pulled.
    client = ollama.Client(host=ollamaHost)
    try:
        listed = client.list()
    except Exception:  # noqa: BLE001 — server down / unreachable
        raise RuntimeError(
            f"Could not reach the Ollama server at {ollamaHost}. "
            f"Start it with:\n    ollama serve")

    names = {m.get("model") or m.get("name", "") for m in listed.get("models", [])}
    # Match with or without an explicit ":latest" tag.
    have = any(n == ollama_model or n.split(":")[0] == ollama_model.split(":")[0]
               for n in names)
    if not have:
        raise RuntimeError(
            f"The Ollama model '{ollama_model}' is not pulled. Get it with:\n"
            f"    ollama pull {ollama_model}")
    return client


def get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        # CPU + int8 is the portable choice on Apple Silicon (no Metal in
        # CTranslate2). This is the slow part on a MacBook Air.
        _WHISPER = WhisperModel(whisperModel, device="cpu", compute_type="int8")
    return _WHISPER


def transcribe_local(video_path):
    """Return the spoken-word transcript via faster-whisper."""
    model = get_whisper()
    segments, _info = model.transcribe(video_path)
    return " ".join(seg.text.strip() for seg in segments).strip()


def extract_frames(video_path, n, out_dir):
    """Extract n evenly-spaced JPEG frames with ffmpeg. Returns sorted paths."""
    duration = video_duration(video_path)
    frame_paths = []

    if duration and duration > 0:
        # Sample at the midpoint of each of n equal time slices.
        for i in range(n):
            ts = duration * (i + 0.5) / n
            out = os.path.join(out_dir, f"frame_{i:03d}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-ss", f"{ts:.3f}", "-i", video_path,
                     "-frames:v", "1",
                     "-vf", f"scale='min({frameMaxWidth},iw)':-2",
                     "-q:v", "3", "-y", out],
                    capture_output=True, timeout=60, check=True,
                )
                if os.path.exists(out):
                    frame_paths.append(out)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
    else:
        # Duration unknown — fall back to a single ffmpeg select pass.
        out_pattern = os.path.join(out_dir, "frame_%03d.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-i", video_path,
                 "-vf", f"thumbnail,fps=1,scale='min({frameMaxWidth},iw)':-2",
                 "-frames:v", str(n),
                 "-q:v", "3", "-y", out_pattern],
                capture_output=True, timeout=120, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        frame_paths = sorted(glob.glob(os.path.join(out_dir, "frame_*.jpg")))

    return sorted(frame_paths)


def analyze_video_local(ollama_client, ollama_model, video_path, frame_count):
    """Local equivalent of analyze_video_gemini. Returns the same JSON keys."""
    transcript = transcribe_local(video_path)

    tmp_dir = tempfile.mkdtemp(prefix="reel_frames_")
    try:
        frames = extract_frames(video_path, frame_count, tmp_dir)
        if not frames:
            raise RuntimeError("ffmpeg extracted no frames from the video")

        images = []
        for fp in frames:
            with open(fp, "rb") as f:
                images.append(base64.b64encode(f.read()).decode("ascii"))

        prompt = localPromptTemplate.format(
            transcript=transcript or "(no speech detected)")
        resp = ollama_client.chat(
            model=ollama_model,
            messages=[{"role": "user", "content": prompt, "images": images}],
            format="json",  # ask Ollama to constrain output to JSON
            options={"num_ctx": ollamaNumCtx},  # default 4096 is too small
        )
        result = parse_json(resp["message"]["content"])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # We trust Whisper's transcript over whatever the VL model echoed back.
    if isinstance(result, dict):
        result["transcript"] = transcript
    return result



# Main


def parse_args():
    p = argparse.ArgumentParser(description="Analyze saved Instagram reels.")
    p.add_argument("--backend", choices=["gemini", "local"], default="gemini",
                   help="gemini (default, needs API key) or local (offline).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report how many reels would be analyzed, then exit.")
    p.add_argument("--limit", type=int, default=None,
                   help="Analyze at most N new reels this run.")
    p.add_argument("--only", metavar="CODE", default=None,
                   help="Analyze only the reel with this shortcode.")
    p.add_argument("--frames", type=int, default=defaultFrameCount,
                   help=f"[local] frames to sample (default {defaultFrameCount}).")
    p.add_argument("--ollama-model", default=defaultOllamaModel,
                   help=f"[local] Ollama VL model (default {defaultOllamaModel}).")
    return p.parse_args()


def code_for(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return shortcode_from_html(path, f.read()) or os.path.basename(path)


def main():
    args = parse_args()

    cache = load_cache()
    all_html = glob.glob(os.path.join(htmlDir, "*.html"))
    if not all_html:
        sys.exit(f"No .html files found in {htmlDir}/")

    # Keep only files whose shortcode looks like a real reel; drop listing
    # pages (reels.html) and placeholders (XXXXXXXXXXX.html).
    html_files = [p for p in all_html if is_reel_shortcode(code_for(p))]
    skipped = len(all_html) - len(html_files)
    if skipped:
        print(f"  (skipping {skipped} non-reel file(s) in {htmlDir}/)")

    # Newest-first ordering. Instagram shortcodes are monotonic with creation
    # time, so reverse-sorting by shortcode puts the newest reels first.
    html_files.sort(key=code_for, reverse=True)

    pending = [p for p in html_files if code_for(p) not in cache]

    # --only narrows to a single reel (even if already cached, so you can
    # re-inspect it by clearing its cache entry).
    if args.only:
        pending = [p for p in html_files if code_for(p) == args.only]
        if not pending:
            sys.exit(f"No reel matching shortcode '{args.only}' in {htmlDir}/")
        pending = [p for p in pending if code_for(p) not in cache]

    if args.limit is not None:
        pending = pending[:args.limit]

    if args.dry_run:
        print(f"Backend: {args.backend}")
        print(f"{len(html_files)} reel(s) total, {len(cache)} already cached.")
        print(f"Would analyze {len(pending)} new reel(s).")
        sys.exit(0)

    # Backend setup.
    gemini_client = None
    ollama_client = None
    if args.backend == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            sys.exit("Set GEMINI_API_KEY first (or use --backend local).")
        from google import genai
        gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    else:
        # Fail fast (before downloading anything) if the local stack is broken.
        try:
            ollama_client = check_local_deps(args.ollama_model)
        except RuntimeError as e:
            sys.exit(f"Local backend not ready:\n{e}")

    # Convert cookies once; reuse the Netscape file for every yt-dlp call.
    cookie_file = prepare_cookies()

    print(f"Backend: {args.backend}. Found {len(html_files)} reel(s). "
          f"Cached: {len(cache)}. New to do: {len(pending)}\n")
    analyzed_now = 0

    try:
        for path in tqdm(pending, desc="Processing reels", unit="reel"):
            with open(path, encoding="utf-8", errors="ignore") as f:
                html = f.read()
            code = shortcode_from_html(path, html) or os.path.basename(path)

            # --- DOWNLOAD STAGE ---
            if not get_video(path, html, cookie_file):
                tqdm.write(f"[{code}] DOWNLOAD stage failed, skipping")
                continue

            # Gemini caps video length; the local backend doesn't care.
            if args.backend == "gemini":
                too_long = video_too_long(tmpVideo)
                if too_long:
                    tqdm.write(f"[{code}] {too_long:.0f}s exceeds "
                               f"{MAX_VIDEO_SECONDS}s limit, skipping")
                    os.remove(tmpVideo)
                    continue

            # --- ANALYSIS STAGE ---
            try:
                if args.backend == "gemini":
                    result = analyze_video_gemini(gemini_client, tmpVideo)
                else:
                    result = analyze_video_local(
                        ollama_client, args.ollama_model, tmpVideo, args.frames)
            except Exception as e:  # noqa: BLE001
                tqdm.write(f"[{code}] ANALYSIS stage failed ({args.backend}): {e}")
                continue
            finally:
                if os.path.exists(tmpVideo):
                    os.remove(tmpVideo)

            cache[code] = {
                "analysis": result,
                "analyzed_at": time.time(),
                "source_file": path,
                "backend": args.backend,
            }
            save_cache(cache)      # save after each so progress is never lost
            analyzed_now += 1
            tqdm.write(f"[{code}] done: {result.get('summary', '')[:60]}")
    finally:
        # Clean up the temp Netscape cookie file (it holds session secrets).
        if cookie_file and os.path.exists(cookie_file):
            os.remove(cookie_file)

    print("\n" + "-" * 50)
    print(f"Backend:           {args.backend}")
    print(f"Analyzed this run: {analyzed_now}")
    print(f"Total cached:      {len(cache)}")
    if args.backend == "gemini":
        spent = analyzed_now * estCostPerReel
        print(f"Estimated spend this run: ${spent:.2f} "
              f"(at ${estCostPerReel}/reel — verify against real pricing)")
    else:
        print("Estimated spend this run: $0.00 (fully local)")
    print(f"Results saved in {cacheFile}")


if __name__ == "__main__":
    main()
