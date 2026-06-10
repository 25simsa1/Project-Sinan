"""Rate analyzed reels to build a (features, rating) dataset.

Reads the analyses produced by analyze_reel.py and lets you score each reel
1-5. Ratings are written to their own store (ratings.json) — separate from the
analysis cache, so rating a reel never rewrites the expensive analysis output.

To run:
    python3 rate_reels.py            # rate reels you haven't rated yet
    python3 rate_reels.py --all      # revisit every reel, including rated ones
    python3 rate_reels.py --stats    # just show how many are rated
"""

import sys
import time

import store
from shortcodes import is_reel_shortcode


def show_reel(code, entry, current_rating=None):
    data = entry.get("analysis", {})
    print("\n" + "=" * 60)
    print(f"Reel: {code}")
    if current_rating:
        print(f"(current rating: {current_rating})")
    print("-" * 60)
    print(f"Summary:      {data.get('summary', '') or '(none)'}")
    if data.get("on_screen_text"):
        print(f"On-screen:    {data['on_screen_text']}")
    if data.get("visuals"):
        print(f"Visuals:      {data['visuals']}")
    if data.get("purpose"):
        print(f"Purpose:      {data['purpose']}")
    print("-" * 60)


def main():
    analysis = store.load_analysis()
    if not analysis:
        sys.exit(f"No {store.ANALYSIS_FILE} found. Run analyze_reel.py first.")
    ratings = store.load_ratings()

    if "--stats" in sys.argv:
        n = len(analysis)
        rated = sum(1 for c in analysis if c in ratings)
        print(f"{n} reel(s) analyzed, {rated} rated, {n - rated} unrated.")
        return

    rate_all = "--all" in sys.argv
    work = {c: e for c, e in analysis.items()
            if is_reel_shortcode(c) and (rate_all or c not in ratings)}

    if not work:
        print("Nothing to rate. (Use --all to revisit rated reels.)")
        return

    print(f"{len(work)} reel(s) to rate. Enter 1-5, 's' to skip, 'q' to quit.")
    rated_now = 0
    for code, entry in work.items():
        current = ratings.get(code, {}).get("rating")
        show_reel(code, entry, current)
        choice = input("Rate 1-5 (s=skip, q=quit): ").strip().lower()
        if choice == "q":
            break
        if choice == "s":
            continue
        if choice.isdigit() and 1 <= int(choice) <= 5:
            ratings[code] = {"rating": int(choice), "rated_at": time.time()}
            store.save_ratings(ratings)  # save after each so nothing is lost
            rated_now += 1
        else:
            print("  not 1-5, skipping.")

    print(f"\nRated this run: {rated_now}. Total rated: {len(ratings)}.")


if __name__ == "__main__":
    main()
