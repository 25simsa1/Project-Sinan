"""Shared shortcode validation, used by every stage that resolves a reel code.

Keeping this in one place means the collector, analyzer, and rater all agree on
what counts as a real reel — so junk saved pages (reels.html -> "reels") and
placeholders (XXXXXXXXXXX.html) can't leak into the pipeline from any entrypoint.
"""

import re


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
