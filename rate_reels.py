"""Rate analyzed reels to build a (features, rating) dataset.

Reads the analysis cache produced by analyze_reel.py, shows each reel's
summary, and lets you score it 1-5. Ratings are written back into the same
cache under "user_rating", so the next analysis run keeps them.

To run:
    python3 rate_reels.py            # rate reels you haven't rated yet
    python3 rate_reels.py --all      # revisit every reel, including rated ones
    python3 rate_reels.py --stats    # just show how many are rated
"""

import os
import sys
import json

CACHE_FILE = "analysis_cache.json"


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            sys.exit("Cache file unreadable — nothing to rate.")
    sys.exit(f"No {CACHE_FILE} found. Run analyze_reel.py first.")


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def show_reel(code, entry):
    data = entry.get("analysis", {})
    print("\n" + "=" * 60)
    print(f"Reel: {code}")
    if entry.get("user_rating"):
        print(f"(current rating: {entry['user_rating']})")
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
    cache = load_cache()

    if "--stats" in sys.argv:
        rated = sum(1 for e in cache.values() if e.get("user_rating"))
        print(f"{len(cache)} reel(s) cached, {rated} rated, "
              f"{len(cache) - rated} unrated.")
        return

    rate_all = "--all" in sys.argv
    work = {c: e for c, e in cache.items()
            if rate_all or not e.get("user_rating")}

    if not work:
        print("Nothing to rate. (Use --all to revisit rated reels.)")
        return

    print(f"{len(work)} reel(s) to rate. Enter 1-5, 's' to skip, 'q' to quit.")
    rated_now = 0
    for code, entry in work.items():
        show_reel(code, entry)
        choice = input("Rate 1-5 (s=skip, q=quit): ").strip().lower()
        if choice == "q":
            break
        if choice == "s":
            continue
        if choice.isdigit() and 1 <= int(choice) <= 5:
            entry["user_rating"] = int(choice)
            save_cache(cache)  # save after each so progress is never lost
            rated_now += 1
        else:
            print("  not 1-5, skipping.")

    total_rated = sum(1 for e in cache.values() if e.get("user_rating"))
    print(f"\nRated this run: {rated_now}. Total rated: {total_rated}.")


if __name__ == "__main__":
    main()
