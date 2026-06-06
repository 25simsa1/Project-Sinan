#!/usr/bin/env python3
"""
Instagram Reel HTML & Video Saver 
Direct Playwright Implementation
"""

import asyncio
import random
import json
from pathlib import Path
from playwright.async_api import async_playwright

# con
URLS = [
    "https://www.instagram.com/reels/DYrOxsxONyt/",
    # Add URLs here
]
OUTPUT_DIR = Path("ig_pages")
COOKIE_FILE = "ig_cookies.json"
DELAY_SECONDS = (8, 18)  # random range
HEADLESS = False  # MUST be False to avoid detection


async def save_login():
    """One-time login to save cookies"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.instagram.com/")
        print("Log in manually in the browser window.")
        input("Press Enter after you're logged in...")
        await context.storage_state(path=COOKIE_FILE)
        print(f"Cookies saved to {COOKIE_FILE}")
        await browser.close()

async def scrape_reels():
    OUTPUT_DIR.mkdir(exist_ok=True)
    
    async with async_playwright() as p:
        # Load saved cookies
        context = await p.chromium.launch_persistent_context(
            user_data_dir=Path("./temp_profile"),
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0"
        )
        
        # apply saved cookies
        if Path(COOKIE_FILE).exists():
            with open(COOKIE_FILE) as f:
                cookies = json.load(f).get("cookies", [])
            await context.add_cookies(cookies)
        
        page = await context.new_page()
        
        for idx, url in enumerate(URLS, 1):
            print(f"\n[{idx}/{len(URLS)}] {url}")
            
            try:
                await page.goto(url, timeout=30000, wait_until="networkidle")
                await asyncio.sleep(3)  # let video player load
                
                # Save HTML
                html = await page.content()
                shortcode = url.rstrip("/").split("/")[-1]
                html_path = OUTPUT_DIR / f"{shortcode}.html"
                html_path.write_text(html, encoding="utf-8")
                print(f"  Saved HTML -> {html_path}")
                
                # Try to get video URL
                video_src = await page.evaluate("""() => {
                    const v = document.querySelector('video');
                    return v ? v.src : null;
                }""")
                if video_src and not video_src.startswith("blob:"):
                    print(f"  Video URL: {video_src[:80]}...")
                    # Optionally download with requests later
                
                # Screenshot (optional)
                # await page.screenshot(path=OUTPUT_DIR / f"{shortcode}.png")
                
            except Exception as e:
                print(f"  ERROR: {e}")
            
            # Delay between requests
            if idx < len(URLS):
                delay = random.uniform(*DELAY_SECONDS)
                print(f"  Waiting {delay:.1f}s...")
                await asyncio.sleep(delay)
        
        await context.close()

if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        asyncio.run(save_login())
    else:
        asyncio.run(scrape_reels())
