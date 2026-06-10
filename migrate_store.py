"""One-time migration: split the old combined analysis_cache.json into the
three separate stores defined in store.py. Safe to run more than once — it
only moves keys it hasn't already moved, and skips cleanly if there's nothing
left to split.

Old shape (everything in one file):
    code -> {analysis, analyzed_at, source_file, backend,
             user_rating, predicted_rating, prediction_reasoning}

After running:
    analysis_cache.json   keeps only analysis fields
    ratings.json          gets {rating, rated_at}
    predictions.json      gets {score, reasoning, model, predicted_at}
"""

import store

MOVED_KEYS = ("user_rating", "predicted_rating", "prediction_reasoning")


def main():
    cache = store.load_analysis()
    ratings = store.load_ratings()
    predictions = store.load_predictions()

    moved_r = moved_p = 0
    for code, entry in cache.items():
        if "user_rating" in entry and code not in ratings:
            ratings[code] = {
                "rating": entry["user_rating"],
                "rated_at": entry.get("rated_at"),  # old data had none
            }
            moved_r += 1
        if "predicted_rating" in entry and code not in predictions:
            predictions[code] = {
                "score": entry["predicted_rating"],
                "reasoning": entry.get("prediction_reasoning", ""),
                "model": entry.get("prediction_model"),
                "predicted_at": entry.get("predicted_at"),
            }
            moved_p += 1
        for k in MOVED_KEYS:
            entry.pop(k, None)

    store.save_analysis(cache)
    store.save_ratings(ratings)
    store.save_predictions(predictions)

    print(f"Moved {moved_r} rating(s)  -> {store.RATINGS_FILE}")
    print(f"Moved {moved_p} prediction(s) -> {store.PREDICTIONS_FILE}")
    print(f"Stripped rating/prediction keys from {store.ANALYSIS_FILE}")
    print(f"analysis_cache: {len(cache)} reel(s), "
          f"ratings: {len(ratings)}, predictions: {len(predictions)}")


if __name__ == "__main__":
    main()
