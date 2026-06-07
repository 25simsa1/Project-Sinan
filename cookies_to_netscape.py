#!/usr/bin/env python3
"""
CLI wrapper: convert Playwright cookies (ig_cookies.json from the collector)
into the Netscape cookies.txt format that yt-dlp / curl understand.

The actual conversion lives in cookies.py; this is just a command-line front
end so you can produce a cookies.txt by hand.

Usage:
    python3 cookies_to_netscape.py                      # ig_cookies.json -> ig_cookies.txt
    python3 cookies_to_netscape.py in.json out.txt      # custom paths
"""

import sys
from pathlib import Path

from cookies import playwright_json_to_netscape


def main():
    in_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ig_cookies.json")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ig_cookies.txt")

    if not in_path.exists():
        sys.exit(f"Input file not found: {in_path}")

    n = playwright_json_to_netscape(in_path, out_path)
    if not n:
        sys.exit("No cookies found in the input file.")
    print(f"Wrote {n} cookie(s) to {out_path}")


if __name__ == "__main__":
    main()
