#!/usr/bin/env python3
"""
Instagram reel collector, feed, saved, or hashtag.
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


# Configuration

cookie_File = Path("ig_cookies.json")
output_Dir = Path("ig_pages")
video_Dir = Path("videos")
user_Agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
nav_Timeout = 30000
scroll_Delay = (1.5, 3.5)
reel_Delay = (4, 10)


# Helpers
def shortcode_from_url(url):
    match = re.search(r'/reels?/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else None

async def save_page_html(page, url, output_Dir):
    code = shortcode_from_url(url)
    if not code:
        code = url.rstrip('/').split('/')[-1]
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
    video_Dir.mkdir(exist_ok=True)
    dest = video_Dir / f"{code}.mp4"
    try:
        subprocess.run(
            ["yt-dlp", url, "--cookies", str(browser_cookies_file),
             "-o", str(dest), "--quiet", "--no-warnings"],
            check=True, timeout=300
        )
        return dest.exists()
    except Exception as e:
        print(f"  Download failed: {e}")
        return False


# Collectors
async def collect_feed_urls(page, limit):
    urls = set()
    print(f"Scrolling feed, target {limit} reels...")
    scrolls = 0
    while len(urls) < limit and scrolls < 20:
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(el => el.href)"
        )
        for link in links:
            if "/reel/" in link or "/reels/" in link:
                urls.add(link.split("?")[0])
                if len(urls) >= limit:
                    break
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(random.uniform(*scroll_Delay))
        scrolls += 1
    print(f"  Found {len(urls)} unique reels.")
    return list(urls)[:limit]

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


# Main
async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="Save login cookies")
    parser.add_argument("--mode", choices=["feed", "saved", "hashtag"], default="feed")
    parser.add_argument("--tag", help="Hashtag (without #) for hashtag mode")
    parser.add_argument("--limit", type=int, default=20, help="Max reels to collect")
    parser.add_argument("--download", action="store_true", help="Download videos using yt-dlp")
    args = parser.parse_args()

    if args.login:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(user_Agent=user_Agent)
            page = await context.new_page()
            await page.goto("https://www.instagram.com/")
            print("Log in to Instagram in the browser window.")
            input("Press Enter after login...")
            await context.storage_state(path=str(cookie_File))
            print(f"Cookies saved to {cookie_File}")
            await browser.close()
        return

    if not cookie_File.exists():
        print("No cookie file. Run with --login first.")
        return

    output_Dir.mkdir(exist_ok=True)
    if args.download:
        video_Dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=Path("./temp_profile"),
            headless=False,
            viewport={"width": 1280, "height": 900},
            user_Agent=user_Agent,
        )
        with open(cookie_File, "r") as f:
            cookies_data = json.load(f)
        await context.add_cookies(cookies_data.get("cookies", []))

        page = await context.new_page()
        await page.goto("https://www.instagram.com/")
        try:
            await page.wait_for_selector('a[href="/direct/inbox/"]', timeout=10000)
            print("Logged in.")
        except PlaywrightTimeout:
            print("Login failed. Run --login again.")
            await context.close()
            return

        if args.mode == "feed":
            urls = await collect_feed_urls(page, args.limit)
        elif args.mode == "saved":
            # Requires username; we extract from page
            profile_link = await page.get_attribute('a[href^="/"]', 'href')
            username = profile_link.strip('/') if profile_link else "your_username"
            saved_url = f"https://www.instagram.com/{username}/saved/all/"
            await page.goto(saved_url)
            await page.wait_for_selector('article a[href*="/reel/"]', timeout=10000)
            urls = await collect_saved_urls(page, args.limit)
        else:  # hashtag
            if not args.tag:
                print("Hashtag mode requires --tag")
                return
            urls = await collect_hashtag_urls(page, args.tag, args.limit)

        print(f"\nProcessing {len(urls)} reels...\n")
        for i, url in enumerate(urls, 1):
            code, ok = await save_page_html(page, url, output_Dir)
            print(f"[{i}/{len(urls)}] {code} - {'OK' if ok else 'FAIL'}")
            if args.download and ok:
                # extract video URL from HTML if needed, or just use yt-dlp on the reel URL
                # simplest: yt-dlp handles it directly with cookies
                video_Dir.mkdir(exist_ok=True)
                dest = video_Dir / f"{code}.mp4"
                try:
                    subprocess.run(
                        ["yt-dlp", url, "--cookies", str(cookie_File),
                         "-o", str(dest), "--quiet", "--no-warnings"],
                        check=True, timeout=300
                    )
                    print(f"    Video saved: {dest}")
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