#!/usr/bin/env python3
"""Canonical cookie conversion shared by the collector and the analyzer.

yt-dlp's --cookies flag only accepts the Netscape cookies.txt format, but the
scrapers save cookies as a Playwright storage_state JSON (ig_cookies.json).
This module is the single place that converts between the two, so the logic
lives in exactly one spot.
"""

import atexit
import json
import os
import tempfile
from pathlib import Path


def playwright_json_to_netscape(json_path, out_path):
    """Convert a Playwright storage_state cookie JSON to a Netscape cookies.txt.

    Returns the number of cookies written (0 if the file is missing, empty, or
    unparseable). 0 is falsy, so callers can write
    `if playwright_json_to_netscape(...):`.

    Handles the #HttpOnly_ domain prefix, session cookies (expires -1 or absent
    -> 0), and the secure / include-subdomains flags.
    """
    if not Path(json_path).exists():
        return 0
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0

    # storage_state() wraps cookies in {"cookies": [...]}; tolerate a bare list.
    cookies = data.get("cookies", []) if isinstance(data, dict) else data
    if not cookies:
        return 0

    lines = ["# Netscape HTTP Cookie File",
             "# Generated from Playwright storage_state by cookies.py", ""]
    written = 0
    for c in cookies:
        name = c.get("name")
        domain = c.get("domain")
        if not name or not domain:
            continue
        value = c.get("value", "")
        path = c.get("path", "/")
        # A leading-dot domain means "valid for subdomains".
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        # Playwright uses -1 (or absent) for session cookies; Netscape wants 0.
        expires = c.get("expires", 0)
        try:
            expiry = int(float(expires)) if expires and float(expires) > 0 else 0
        except (TypeError, ValueError):
            expiry = 0
        # yt-dlp honours the #HttpOnly_ domain prefix for httpOnly cookies.
        domain_field = ("#HttpOnly_" + domain) if c.get("httpOnly") else domain
        lines.append("\t".join([domain_field, include_sub, path, secure,
                                str(expiry), name, value]))
        written += 1

    if not written:
        return 0
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return written


_NETSCAPE_CACHE = {}  # json path -> temp netscape path (per process)


def netscape_cookie_file(json_path):
    """Convert json_path to a temp Netscape cookies.txt for yt-dlp.

    Converts once per process (cached) and auto-deletes the temp file at exit.
    Returns the temp path, or None if the file is missing / has no usable
    cookies. The temp file holds session secrets, so it lives outside the repo
    and is removed at interpreter exit.
    """
    key = str(json_path)
    if key in _NETSCAPE_CACHE:
        return _NETSCAPE_CACHE[key]
    if not Path(json_path).exists():
        return None
    fd, tmp = tempfile.mkstemp(prefix="yt_dlp_cookies_", suffix=".txt")
    os.close(fd)
    if playwright_json_to_netscape(json_path, tmp):
        _NETSCAPE_CACHE[key] = tmp
        atexit.register(lambda: os.path.exists(tmp) and os.remove(tmp))
        return tmp
    os.remove(tmp)
    return None
