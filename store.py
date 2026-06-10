"""Central data stores for the reel pipeline.

Three separate JSON files, all keyed by the 11-char reel shortcode:

  analysis_cache.json   code -> {analysis, analyzed_at, source_file, backend}
                        Written once per reel by analyze_reel.py. The expensive,
                        immutable output of the analysis step.
  ratings.json          code -> {rating, rated_at}
                        Your feedback — the ground-truth signal, written by
                        rate_reels.py.
  predictions.json      code -> {score, reasoning, model, predicted_at}
                        Model guesses, written by predict_rating.py.

Keeping them separate means rating or re-scoring a reel never rewrites (or risks
clobbering) the analysis output, and predictions can be wiped and rebuilt after
retraining without touching your ratings. A reel can exist in analysis with no
rating (not seen yet) or no prediction (not scored yet).
"""

import json
import os

ANALYSIS_FILE = "analysis_cache.json"
RATINGS_FILE = "ratings.json"
PREDICTIONS_FILE = "predictions.json"


def _load(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"  ({path} unreadable, treating as empty)")
    return {}


def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_analysis():
    return _load(ANALYSIS_FILE)


def save_analysis(data):
    _save(ANALYSIS_FILE, data)


def load_ratings():
    return _load(RATINGS_FILE)


def save_ratings(data):
    _save(RATINGS_FILE, data)


def load_predictions():
    return _load(PREDICTIONS_FILE)


def save_predictions(data):
    _save(PREDICTIONS_FILE, data)
