"""Benchmark prompt-injection detection across four public datasets.

    python -m evals.injection_benchmark

Two questions, answered separately:

1. In distribution: train on every dataset's dev split, score each held-out test split.
2. **Unseen data** (the honest "real world" proxy): leave one dataset out entirely, train on the
   rest, score the left-out dataset's test split. Plus Gandalf, real attacks never trained on.

Every model is combined with the rules (block if either fires). Each model's threshold is chosen
from out-of-fold predictions on its own training data at <= 1% false positives; test rows are
never used to train or to choose a threshold.
"""

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
from scipy.sparse import hstack
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from app.injection import InjectionDetector, normalize
from app.injection_model import HEAD_TAIL_CHARS, VECTORIZER_PARAMS, build_vectorizers, head_and_tail
from app.schemas import ChatMessage
from evals.injection_data import SCORED, Case, load_all

MAX_FALSE_POSITIVE_RATE = 0.01
SEED = 7
CACHE = Path(__file__).parent / ".cache"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# The embedding model reads ~128 tokens, so each text is embedded as a head and a tail chunk.
CHUNK_CHARS = 600


# --- Features ----------------------------------------------------------------------------


class Tfidf:
    name = "tfidf"

    def fit_transform(self, texts):
        self.vectorizers = build_vectorizers()
        clipped = [normalize(head_and_tail(t, HEAD_TAIL_CHARS)) for t in texts]
        return hstack([self.vectorizers[n].fit_transform(clipped) for n in VECTORIZER_PARAMS]).tocsr()

    def transform(self, texts):
        clipped = [normalize(head_and_tail(t, HEAD_TAIL_CHARS)) for t in texts]
        return hstack([self.vectorizers[n].transform(clipped) for n in VECTORIZER_PARAMS]).tocsr()


class Embedding:
    """Head and tail chunks embedded separately and concatenated (768 dims). Fixed features, so
    fit_transform needs nothing from the training data; vectors are cached on disk."""

    name = "embedding"
    _model = None

    def _embed(self, chunks: list[str]) -> np.ndarray:
        CACHE.mkdir(exist_ok=True)
        cache_file = CACHE / "embeddings.npz"
        cache = dict(np.load(cache_file)) if cache_file.exists() else {}
        keys = [hashlib.sha256(c.encode()).hexdigest()[:24] for c in chunks]
        missing = sorted({k: c for k, c in zip(keys, chunks, strict=True) if k not in cache}.items())
        if missing:
            if Embedding._model is None:
                from fastembed import TextEmbedding

                Embedding._model = TextEmbedding(EMBEDDING_MODEL)
            vectors = list(Embedding._model.embed([c for _, c in missing], batch_size=64))
            cache.update({k: v for (k, _), v in zip(missing, vectors, strict=True)})
            np.savez(cache_file, **cache)
        return np.stack([cache[k] for k in keys])

    def transform(self, texts):
        heads = [t[:CHUNK_CHARS] for t in texts]
        tails = [t[-CHUNK_CHARS:] for t in texts]
        return np.hstack([self._embed(heads), self._embed(tails)])

    def fit_transform(self, texts):
        return self.transform(texts)


# --- Training and threshold --------------------------------------------------------------


def fit_model(featurizer_factory: Callable, texts: list[str], labels: np.ndarray):
    featurizer = featurizer_factory()
    model = LogisticRegression(C=4.0, class_weight="balanced", max_iter=3000, random_state=SEED)
    model.fit(featurizer.fit_transform(texts), labels)
    return featurizer, model


def choose_threshold(featurizer_factory: Callable, texts: list[str], labels: np.ndarray) -> tuple[float, dict]:
    """Highest recall at <= MAX_FALSE_POSITIVE_RATE, from out-of-fold predictions.

    Returns the threshold and what was actually measured at it (not the budget)."""
    oof = np.zeros(len(texts))
    for train_idx, valid_idx in StratifiedKFold(5, shuffle=True, random_state=SEED).split(texts, labels):
        featurizer, model = fit_model(featurizer_factory, [texts[i] for i in train_idx], labels[train_idx])
        oof[valid_idx] = model.predict_proba(featurizer.transform([texts[i] for i in valid_idx]))[:, 1]
    best, stats = 1.0, {"cv_recall": -1.0, "cv_false_positive_rate": 0.0}
    for threshold in np.unique(np.round(oof, 4)):
        flagged = oof >= threshold
        fp = (flagged & (labels == 0)).sum() / max(1, (labels == 0).sum())
        recall = (flagged & (labels == 1)).sum() / max(1, (labels == 1).sum())
        if fp <= MAX_FALSE_POSITIVE_RATE and recall > stats["cv_recall"]:
            best = float(threshold)
            stats = {"cv_recall": round(float(recall), 4), "cv_false_positive_rate": round(float(fp), 4)}
    return best, stats


class Scorer:
    """Rules OR model. Rule verdicts are computed once per text and reused."""

    def __init__(self):
        self.rules = InjectionDetector()
        self._rule_cache: dict[str, bool] = {}

    def rule_blocks(self, text: str) -> bool:
        if text not in self._rule_cache:
            self._rule_cache[text] = self.rules.scan([ChatMessage(role="user", content=text)]).blocked
        return self._rule_cache[text]

    def score(self, cases: list[Case], model=None) -> dict:
        texts = [c.text for c in cases]
        labels = np.array([c.label for c in cases])
        flagged = np.array([self.rule_blocks(t) for t in texts])
        if model is not None:
            featurizer, classifier, threshold = model
            flagged |= classifier.predict_proba(featurizer.transform(texts))[:, 1] >= threshold
        injections, benign = (labels == 1).sum(), (labels == 0).sum()
        return {
            "recall": float((flagged & (labels == 1)).sum() / injections) if injections else None,
            "fp_rate": float((flagged & (labels == 0)).sum() / benign) if benign else None,
            "n": f"{injections}+{benign}",
        }


def train(featurizer_factory: Callable, cases: list[Case]):
    texts = [c.text for c in cases]
    labels = np.array([c.label for c in cases])
    threshold, _ = choose_threshold(featurizer_factory, texts, labels)
    featurizer, classifier = fit_model(featurizer_factory, texts, labels)
    return featurizer, classifier, threshold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["tfidf", "embedding"])
    args = parser.parse_args()
    data = load_all()
    dev = {name: [c for c in cases if c.split == "dev"] for name, cases in data.items()}
    test = {name: [c for c in cases if c.split == "test"] for name, cases in data.items()}
    factories = {"tfidf": Tfidf, "embedding": Embedding}
    scorer = Scorer()
    report = {"in_distribution": {}, "unseen": {}}

    # 1. In distribution: train on all dev data.
    all_dev = [c for cases in dev.values() for c in cases]
    for target in [*SCORED, "gandalf"]:
        report["in_distribution"].setdefault("rules", {})[target] = scorer.score(test[target])
    for name in args.models:
        model = train(factories[name], all_dev)
        for target in [*SCORED, "gandalf"]:
            report["in_distribution"].setdefault(f"rules+{name}", {})[target] = scorer.score(test[target], model)

    # 2. Unseen data: leave one dataset out of training entirely.
    for target in SCORED:
        report["unseen"].setdefault("rules", {})[target] = scorer.score(test[target])
        pool = [c for name, cases in dev.items() if name != target for c in cases]
        for name in args.models:
            model = train(factories[name], pool)
            report["unseen"].setdefault(f"rules+{name}", {})[target] = scorer.score(test[target], model)
    report["unseen"]["rules"]["gandalf"] = report["in_distribution"]["rules"]["gandalf"]
    for name in args.models:
        report["unseen"][f"rules+{name}"]["gandalf"] = report["in_distribution"][f"rules+{name}"]["gandalf"]

    for section, title in (
        ("in_distribution", "IN DISTRIBUTION (trained on every dataset's dev split)"),
        ("unseen", "UNSEEN (target dataset left out of training; Gandalf never trained on)"),
    ):
        print(f"\n{title}")
        print(f"  {'detector':<22}" + "".join(f"{t:>22}" for t in [*SCORED, "gandalf"]) + f"{'mean recall':>14}")
        for detector, results in report[section].items():
            cells, recalls = [], []
            for target in [*SCORED, "gandalf"]:
                r = results[target]
                recalls.append(r["recall"])
                fp = "" if r["fp_rate"] is None else f" / FP {r['fp_rate']:.1%}"
                cells.append(f"{r['recall']:.0%}{fp}")
            print(f"  {detector:<22}" + "".join(f"{c:>22}" for c in cells) + f"{np.mean(recalls):>13.0%}")
    out = Path(__file__).parent / "results" / "injection_benchmark.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nsaved {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
