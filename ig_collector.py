#!/usr/bin/env python3
""" Instagram reel collector, feed, saved, or hashtag.
Saves HTML and optionally downloads videos.

Usage:
    python ig_collector.py --login
    python ig_collector.py --mode feed --limit 20
    python ig_collector.py --mode saved --limit 30
    python ig_collector.py --mode hashtag --tag cats --limit 15 --download

Requirements:
    pip install playwright yt-dlp
    playwright install chromium
"""

import asyncio
import json
import random
import re
import sys
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from cookies import netscape_cookie_file
from shortcodes import is_reel_shortcode


# Configuration

cookie_File = Path("ig_cookies.json")
output_Dir = Path("ig_pages")
video_Dir = Path("videos")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
nav_Timeout = 30000
scroll_Delay = (1.5, 3.5)
reel_Delay = (4, 10)


# Cookies — yt-dlp's --cookies only accepts the Netscape cookies.txt format,
# but ig_cookies.json is a Playwright storage_state JSON. The conversion lives
# in cookies.py (netscape_cookie_file converts once, caches, and cleans up the
# temp file on exit).


# Helpers
def shortcode_from_url(url):
    match = re.search(r'/reels?/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else None

async def save_page_html(page, url, output_Dir):
    code = shortcode_from_url(url)
    if not code:
        code = url.rstrip('/').split('/')[-1]
    # Single chokepoint: never write a page that isn't a real reel, so junk
    # like reels.html / XXXXXXXXXXX.html can't enter the pipeline downstream.
    if not is_reel_shortcode(code):
        print(f"  Skipping non-reel {url} (code {code!r})")
        return code, False
    try:
        await page.goto(url, timeout=nav_Timeout, wait_until="networkidle")
        await asyncio.sleep(2)
        html = await page.content()
        output_path = output_Dir / f"{code}.html"
        output_path.write_text(html, encoding="utf-8")
        return code, True
    except Exception as e:
        print(f"  Failed {url}: {e}")
        return code, False

def download_video(url, code, browser_cookies_file):
    if not Path(browser_cookies_file).exists():
        print("  Cookies file missing, cannot download video")
        return False
    cookies = netscape_cookie_file(browser_cookies_file)
    if not cookies:
        print("  Cookies file had no usable cookies, cannot download video")
        return False
    video_Dir.mkdir(exist_ok=True)
    dest = video_Dir / f"{code}.mp4"
    try:
        subprocess.run(
            ["yt-dlp", url, "--cookies", cookies,
             "-o", str(dest), "--quiet", "--no-warnings"],
            check=True, timeout=300
        )
        return dest.exists()
    except Exception as e:
        print(f"  Download failed: {e}")
        return False


async def collect_feed_urls(page, limit):
    """Collect reel URLs from the main home feed."""
    urls = set()
    print(f"Scrolling home feed, target {limit} reels...")
    last_height = 0
    no_new = 0
    scrolls = 0
    max_scrolls = 30

    while len(urls) < limit and scrolls < max_scrolls:
        # Extract all links containing /reel/ from the current DOM
        links = await page.eval_on_selector_all(
            'a[href*="/reel/"]',
            "els => els.map(el => el.href)"
        )
        for link in links:
            clean = link.split("?")[0]
            urls.add(clean)
            if len(urls) >= limit:
                break
        print(f"  Scroll {scrolls+1}: {len(urls)} reels so far")

        # Scroll down (simulate human)
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Wait for page height to increase (new content loaded)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            no_new += 1
            if no_new >= 3:
                print("  No more content loading. Stopping.")
                break
        else:
            no_new = 0
        last_height = new_height
        scrolls += 1

    print(f"  Found {len(urls)} reels.")
    return list(urls)[:limit]


async def collect_reels_tab_urls(page, limit):
    """Collect reel URLs from the dedicated Reels tab using auto-scroll.

    The Reels tab shows one full-screen reel at a time, so there is usually no
    static list of <a href="/reel/"> links to scrape. We let an injected
    auto-scroll script advance through reels, and on each iteration we capture
    the URL two ways for robustness:
      1. page.url (Instagram updates the address bar to /reels/<code>/)
      2. any visible a[href*="/reel/"] anchors in the DOM
    """
    print("Navigating to Reels tab...")
    await page.goto("https://www.instagram.com/reels/", wait_until="networkidle")
    await asyncio.sleep(2)

    # Wait for a video to be present
    await page.wait_for_selector('video', timeout=10000)

    # Inject the auto-scroll script
    auto_scroll_js = """
    (function () {
      const MAX_WATCH_SECONDS = 30;
      const SCROLL_BEHAVIOR = 'smooth';

      function getScrollContainer() {
        const guess = document.querySelector('.x1pq812k.xvbhtw8.x9f619.x1dr59a3');
        if (guess && guess.scrollHeight > guess.clientHeight) return guess;
        const v = document.querySelector('video');
        let el = v;
        while (el && el !== document.body) {
          const style = getComputedStyle(el);
          if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight) {
            return el;
          }
          el = el.parentElement;
        }
        return document.scrollingElement || document.documentElement;
      }

      function goToNextReel() {
        const c = getScrollContainer();
        c.scrollBy({ top: c.clientHeight, behavior: SCROLL_BEHAVIOR });
      }

      let lastTime = 0;
      let watchedThis = 0;
      let advancing = false;

      function tick() {
        const v = document.querySelector('video');
        if (!v) return;
        const looped = v.currentTime < lastTime - 0.5;
        lastTime = v.currentTime;
        watchedThis += 0.25;
        const ended = v.ended;
        const capHit = watchedThis >= MAX_WATCH_SECONDS;
        if ((ended || looped || capHit) && !advancing) {
          advancing = true;
          watchedThis = 0;
          lastTime = 0;
          goToNextReel();
          setTimeout(() => { advancing = false; }, 1500);
        }
      }

      window.__reelScroller = setInterval(tick, 250);
      window.stopReelScroll = function () {
        clearInterval(window.__reelScroller);
        console.log('Auto-scroll stopped.');
      };
      console.log('Auto-scroll running.');
    })();
    """
    await page.evaluate(auto_scroll_js)
    print("Auto-scroll started.")

    # Collect URLs as the auto-scroll advances through reels
    collected_urls = set()
    no_change_count = 0
    max_no_change = 5  # stop after this many idle iterations in a row

    # Give the first reel time to load
    await asyncio.sleep(5)

    while len(collected_urls) < limit and no_change_count < max_no_change:
        before = len(collected_urls)

        # 1. Current address-bar URL
        code = shortcode_from_url(page.url)
        if code:
            collected_urls.add(page.url.split("?")[0])

        # 2. Any reel anchors currently in the DOM
        try:
            links = await page.eval_on_selector_all(
                'a[href*="/reel/"]',
                "els => els.map(el => el.href)"
            )
        except Exception:
            links = []
        for link in links:
            clean = link.split("?")[0]
            if shortcode_from_url(clean):
                collected_urls.add(clean)
            if len(collected_urls) >= limit:
                break

        if len(collected_urls) > before:
            new_count = len(collected_urls) - before
            print(f"  Collected +{new_count} ({len(collected_urls)}/{limit})")
            no_change_count = 0
        else:
            no_change_count += 1

        # Wait for the reel to play and auto-scroll to advance
        await asyncio.sleep(8)

    # Stop the auto-scroll
    try:
        await page.evaluate("window.stopReelScroll()")
    except Exception:
        pass

    print(f"  Found {len(collected_urls)} unique reels.")
    return list(collected_urls)[:limit]


async def collect_saved_urls(page, limit):
    await page.goto("https://www.instagram.com/")
    # Navigate to saved posts
    await page.goto("https://www.instagram.com/your_username/saved/all/", wait_until="networkidle")
    # Wait for grid to load
    await page.wait_for_selector('article a[href*="/reel/"]', timeout=10000)
    urls = set()
    last_height = 0
    while len(urls) < limit:
        links = await page.eval_on_selector_all(
            'article a[href*="/reel/"]',
            "els => els.map(el => el.href)"
        )
        for link in links:
            urls.add(link.split("?")[0])
            if len(urls) >= limit:
                break
        # scroll to load more
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(*scroll_Delay))
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    print(f"  Found {len(urls)} saved reels.")
    return list(urls)[:limit]

async def collect_hashtag_urls(page, tag, limit):
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_selector('article a[href*="/reel/"]', timeout=10000)
    urls = set()
    last_height = 0
    while len(urls) < limit:
        links = await page.eval_on_selector_all(
            'article a[href*="/reel/"]',
            "els => els.map(el => el.href)"
        )
        for link in links:
            urls.add(link.split("?")[0])
            if len(urls) >= limit:
                break
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(*scroll_Delay))
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    print(f"  Found {len(urls)} reels for #{tag}.")
    return list(urls)[:limit]


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="Save login cookies")
    parser.add_argument("--mode", choices=["feed", "saved", "hashtag", "reels"], default="feed",
                        help="Source: feed (home page), saved (saved posts), hashtag, or reels (dedicated tab)")
    parser.add_argument("--tag", help="Hashtag (without #) for hashtag mode")
    parser.add_argument("--limit", type=int, default=20, help="Max reels to collect")
    parser.add_argument("--download", action="store_true", help="Download videos using yt-dlp")
    args = parser.parse_args()

    # Handle login first, then exit
    if args.login:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            await page.goto("https://www.instagram.com/")
            print("Log in to Instagram in the browser window.")
            input("Press Enter after login...")
            await context.storage_state(path=str(cookie_File))
            print(f"Cookies saved to {cookie_File}")
            await browser.close()
        return

    # Ensure cookies exist
    if not cookie_File.exists():
        print("No cookie file. Run with --login first.")
        return

    output_Dir.mkdir(exist_ok=True)
    if args.download:
        video_Dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        # Launch persistent context (or regular context with cookies)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=Path("./temp_profile"),
            headless=False,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        # Load saved cookies
        with open(cookie_File, "r") as f:
            cookies_data = json.load(f)
        await context.add_cookies(cookies_data.get("cookies", []))

        page = await context.new_page()
        await page.goto("https://www.instagram.com/")

        # Verify login
        try:
            await page.wait_for_selector('a[href="/direct/inbox/"]', timeout=10000)
            print("Logged in.")
        except PlaywrightTimeout:
            print("Login failed. Run --login again.")
            await context.close()
            return

        # Collect reel URLs based on mode
        if args.mode == "feed":
            urls = await collect_feed_urls(page, args.limit)
        elif args.mode == "reels":
            urls = await collect_reels_tab_urls(page, args.limit)
        elif args.mode == "saved":
            # Extract username from the current page (assumes you are on instagram.com)
            # Better: use a reliable selector
            profile_link = await page.get_attribute('a[href^="/"]', 'href')
            username = profile_link.strip('/') if profile_link else "your_username"
            saved_url = f"https://www.instagram.com/{username}/saved/all/"
            await page.goto(saved_url, wait_until="networkidle")
            try:
                await page.wait_for_selector('article a[href*="/reel/"]', timeout=10000)
            except PlaywrightTimeout:
                print("No saved reels found or page didn't load correctly.")
                urls = []
            else:
                urls = await collect_saved_urls(page, args.limit)
        elif args.mode == "hashtag":
            if not args.tag:
                print("Hashtag mode requires --tag")
                await context.close()
                return
            urls = await collect_hashtag_urls(page, args.tag, args.limit)
        else:
            urls = []

        if not urls:
            print("No reels found.")
            await context.close()
            return

        print(f"\nProcessing {len(urls)} reels...\n")
        for i, url in enumerate(urls, 1):
            code, ok = await save_page_html(page, url, output_Dir)
            print(f"[{i}/{len(urls)}] {code} - {'OK' if ok else 'FAIL'}")
            if args.download and ok:
                dest = video_Dir / f"{code}.mp4"
                cookies = netscape_cookie_file(cookie_File)
                if not cookies:
                    print("    Video download failed: no usable cookies "
                          "(run --login to refresh ig_cookies.json)")
                    continue
                try:
                    proc = subprocess.run(
                        ["yt-dlp", url, "--cookies", cookies,
                         "-o", str(dest), "--no-warnings"],
                        capture_output=True, text=True, timeout=300
                    )
                    if proc.returncode == 0 and dest.exists():
                        print(f"    Video saved: {dest}")
                    else:
                        err = (proc.stderr or "").strip().lower()
                        if any(s in err for s in
                               ("login required", "rate-limit", "rate limit", "429")):
                            print("    Video download failed: Instagram "
                                  "rate-limit / login wall — cookies may be "
                                  "stale, run --login to refresh them.")
                        else:
                            tail = (proc.stderr or "").strip().splitlines()
                            print(f"    Video download failed: "
                                  f"{tail[-1] if tail else 'unknown yt-dlp error'}")
                except Exception as e:
                    print(f"    Video download failed: {e}")
            if i < len(urls):
                delay = random.uniform(*reel_Delay)
                print(f"    Waiting {delay:.1f}s...")
                await asyncio.sleep(delay)

        await context.close()

    print(f"\nHTML saved in {output_Dir}")
    if args.download:
        print(f"Videos saved in {video_Dir}")

if __name__ == "__main__":
    asyncio.run(main())