"""Train the shipped prompt-injection classifier on dev data only.

    python -m evals.train_injection_classifier

Training data: the "dev" split of every dataset in evals/injection_data.py (deepset, jailbreak,
SPML, hand-written). Gandalf and every "test" split are never read here. The benchmark
(evals/injection_benchmark.py) showed why breadth matters: a classifier tuned to <=1% false
positives on one kind of honest text blocked up to 68% of a different kind it had never seen.

The decision threshold is chosen from out-of-fold predictions (5-fold cross-validation on dev):
the threshold that catches the most injections while keeping the dev false-positive rate at or
below MAX_FALSE_POSITIVE_RATE. Then the model is refit on all dev data and saved as JSON.
"""

import json
from collections import Counter
from datetime import UTC, datetime

import numpy as np

from app.injection_model import MODEL_FILE, InjectionClassifier
from evals.injection_benchmark import MAX_FALSE_POSITIVE_RATE, Tfidf, choose_threshold, fit_model
from evals.injection_data import load_all


def main() -> None:
    dev = [c for cases in load_all().values() for c in cases if c.split == "dev"]
    texts = [c.text for c in dev]
    labels = np.array([c.label for c in dev])

    threshold, cv = choose_threshold(Tfidf, texts, labels)
    featurizer, model = fit_model(Tfidf, texts, labels)
    classifier = InjectionClassifier(featurizer.vectorizers, model.coef_[0], float(model.intercept_[0]), threshold)
    metadata = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "training_rows": len(texts),
        "training_injections": int(labels.sum()),
        "training_rows_by_dataset": dict(Counter(c.dataset for c in dev)),
        "max_false_positive_rate": MAX_FALSE_POSITIVE_RATE,
        **cv,
    }
    classifier.save(MODEL_FILE, metadata)
    print(json.dumps({"threshold": round(threshold, 4), **metadata}, indent=2))
    print(f"saved {MODEL_FILE.name} ({MODEL_FILE.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
