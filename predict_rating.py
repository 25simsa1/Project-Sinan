"""Predict how much you'd like a reel, using a LOCAL model via Ollama.

No cloud API, no cost. Reads analyses from analysis_cache.json and your scores
from ratings.json, builds a short "taste profile" from the reels you already
rated, and asks a local LLM to score each unrated reel 1-5 with a one-line
reason. Predictions are written to their own store (predictions.json), so they
can be wiped and regenerated without touching your ratings or analyses.

Setup (one time):
    # install Ollama: https://ollama.com  (brew install ollama)
    ollama serve            # start the local server (or it runs as a service)
    ollama pull llama3.2    # any chat model works: qwen2.5, mistral, phi...

To run:
    python3 predict_rating.py              # predict every unrated reel
    python3 predict_rating.py --limit 5    # at most 5 this run
    python3 predict_rating.py --model qwen2.5
    python3 predict_rating.py --eval       # test accuracy on reels you HAVE rated
    python3 predict_rating.py --stats
"""

import sys
import time
import json
import urllib.request
import urllib.error

import store

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "llama3.2"

# Need at least this many rated reels before predictions mean anything.
MIN_RATINGS = 5
# How many liked/disliked examples to show the model as taste context.
N_EXAMPLES = 5


def features(entry):
    """The text the model judges on — never the video, just the analysis."""
    a = entry.get("analysis", {})
    parts = []
    for key in ("summary", "visuals", "purpose", "on_screen_text"):
        val = a.get(key)
        if val:
            parts.append(f"{key}: {val}")
    return "\n".join(parts) or "(no analysis text)"


def build_examples(analysis, ratings, exclude=None):
    """Pick the highest- and lowest-rated reels as contrastive examples."""
    rated = [(c, ratings[c]["rating"]) for c in ratings
             if c in analysis and c != exclude]
    rated.sort(key=lambda cr: cr[1])
    disliked = rated[:N_EXAMPLES]
    liked = rated[-N_EXAMPLES:]
    lines = []
    for code, score in liked + disliked:
        lines.append(f"[You rated this {score}/5]\n{features(analysis[code])}")
    return "\n\n".join(lines)


def predict_one(model, examples, target_text):
    system = (
        "You predict how much a specific user will like a short video reel, "
        "on a scale of 1 to 5, based on examples of reels they already rated. "
        'Reply ONLY with JSON: {"reasoning": "<one short sentence>", '
        '"predicted_rating": <integer 1-5>}'
    )
    user = (
        f"Here are reels the user rated:\n\n{examples}\n\n"
        f"Now predict their rating for this new reel:\n\n{target_text}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",          # ask Ollama to constrain output to JSON
        "options": {"temperature": 0},  # deterministic — same reel, same score
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    content = body["message"]["content"]
    data = json.loads(content)
    rating = int(data["predicted_rating"])
    if not 1 <= rating <= 5:
        raise ValueError(f"out-of-range rating {rating}")
    return rating, data.get("reasoning", "")


def check_ollama():
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5)
    except (urllib.error.URLError, OSError):
        sys.exit(
            "Can't reach Ollama at localhost:11434.\n"
            "  Install: https://ollama.com  (brew install ollama)\n"
            "  Start:   ollama serve\n"
            "  Model:   ollama pull llama3.2"
        )


def arg_value(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    analysis = store.load_analysis()
    ratings = store.load_ratings()
    predictions = store.load_predictions()
    model = arg_value("--model", DEFAULT_MODEL)

    # Only ratings we actually have analysis text for are usable.
    rated = {c: r for c, r in ratings.items() if c in analysis}
    if len(rated) < MIN_RATINGS:
        sys.exit(f"Only {len(rated)} rated reel(s); need at least "
                 f"{MIN_RATINGS} before predictions are meaningful. "
                 f"Rate more with rate_reels.py.")

    if "--stats" in sys.argv:
        print(f"{len(analysis)} analyzed, {len(rated)} rated, "
              f"{len(predictions)} have a prediction.")
        return

    check_ollama()

    # --eval: predict reels you ALREADY rated (excluding each from its own
    # examples to avoid leakage) and report mean absolute error.
    if "--eval" in sys.argv:
        errors = []
        for code in rated:
            examples = build_examples(analysis, ratings, exclude=code)
            try:
                pred, _ = predict_one(model, examples, features(analysis[code]))
            except Exception as e:  # noqa: BLE001
                print(f"[{code}] eval failed: {e}")
                continue
            actual = rated[code]["rating"]
            errors.append(abs(pred - actual))
            print(f"[{code}] predicted {pred} vs actual {actual}")
        if errors:
            mae = sum(errors) / len(errors)
            print(f"\nMean absolute error: {mae:.2f} over {len(errors)} reel(s) "
                  f"(0 = perfect, lower is better).")
        return

    # Normal mode: predict reels that are unrated and not yet predicted.
    todo = [c for c in analysis
            if c not in ratings and c not in predictions]
    limit = arg_value("--limit")
    if limit and limit.isdigit():
        todo = todo[:int(limit)]

    if not todo:
        print("Nothing to predict.")
        return

    examples = build_examples(analysis, ratings)
    print(f"Predicting {len(todo)} reel(s) with local model '{model}'...")
    done = 0
    for code in todo:
        try:
            pred, why = predict_one(model, examples, features(analysis[code]))
        except Exception as e:  # noqa: BLE001
            print(f"[{code}] failed: {e}")
            continue
        predictions[code] = {
            "score": pred,
            "reasoning": why,
            "model": model,
            "predicted_at": time.time(),
        }
        store.save_predictions(predictions)  # save after each — never lose work
        done += 1
        print(f"[{code}] predicted {pred}/5 — {why}")

    print(f"\nPredicted this run: {done}.")


if __name__ == "__main__":
    main()
