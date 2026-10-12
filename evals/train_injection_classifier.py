"""Train the prompt-injection classifier on dev data only.

    python -m evals.train_injection_classifier

Training data: the "dev" split of the public dataset (deepset's train split) plus the
hand-written cases (both halves are dev data now). The public "test" split is never read here.

The decision threshold is chosen from out-of-fold predictions (5-fold cross-validation on dev):
the threshold that catches the most injections while keeping the dev false-positive rate at or
below MAX_FALSE_POSITIVE_RATE. Then the model is refit on all dev data and saved as JSON.
"""

import json
from datetime import UTC, datetime

import numpy as np
from scipy.sparse import hstack
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from app.injection import normalize
from app.injection_model import MODEL_FILE, VECTORIZER_PARAMS, InjectionClassifier, build_vectorizers
from evals.injection_eval import CASES_FILE, PUBLIC_FILE

MAX_FALSE_POSITIVE_RATE = 0.01
C = 4.0
SEED = 7


def load_dev() -> tuple[list[str], np.ndarray]:
    texts, labels = [], []
    for path, splits in ((PUBLIC_FILE, {"dev"}), (CASES_FILE, {"dev", "test"})):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            if case["split"] in splits:
                texts.append(case["text"])
                labels.append(1 if case["label"] == "injection" else 0)
    return texts, np.array(labels)


def fit(texts: list[str]) -> tuple[dict, object]:
    normalized = [normalize(t) for t in texts]
    vectorizers = build_vectorizers()
    matrix = hstack([vectorizers[name].fit_transform(normalized) for name in VECTORIZER_PARAMS]).tocsr()
    return vectorizers, matrix


def train(texts: list[str], labels: np.ndarray) -> InjectionClassifier:
    vectorizers, matrix = fit(texts)
    model = LogisticRegression(C=C, class_weight="balanced", max_iter=2000, random_state=SEED)
    model.fit(matrix, labels)
    return InjectionClassifier(vectorizers, model.coef_[0], float(model.intercept_[0]), threshold=0.5)


def choose_threshold(texts: list[str], labels: np.ndarray) -> tuple[float, dict]:
    """Out-of-fold probabilities, so the threshold is chosen on predictions for unseen dev text."""
    out_of_fold = np.zeros(len(texts))
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)
    for train_idx, valid_idx in folds.split(texts, labels):
        classifier = train([texts[i] for i in train_idx], labels[train_idx])
        out_of_fold[valid_idx] = classifier.probabilities([texts[i] for i in valid_idx])

    best = None
    for threshold in np.unique(np.round(out_of_fold, 4)):
        predicted = out_of_fold >= threshold
        fp_rate = (predicted & (labels == 0)).sum() / (labels == 0).sum()
        recall = (predicted & (labels == 1)).sum() / (labels == 1).sum()
        if fp_rate <= MAX_FALSE_POSITIVE_RATE and (best is None or recall > best[1]):
            best = (float(threshold), float(recall), float(fp_rate))
    threshold, recall, fp_rate = best
    return threshold, {"cv_recall": round(recall, 4), "cv_false_positive_rate": round(fp_rate, 4)}


def main() -> None:
    texts, labels = load_dev()
    threshold, cv = choose_threshold(texts, labels)
    classifier = train(texts, labels)
    classifier.threshold = threshold
    metadata = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "training_rows": len(texts),
        "training_injections": int(labels.sum()),
        "max_false_positive_rate": MAX_FALSE_POSITIVE_RATE,
        "C": C,
        **cv,
    }
    classifier.save(MODEL_FILE, metadata)
    print(json.dumps({"threshold": round(threshold, 4), **metadata}, indent=2))
    print(f"saved {MODEL_FILE.name} ({MODEL_FILE.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
