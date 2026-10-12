"""A small prompt-injection classifier: the second layer behind the rules (DESIGN.md §4).

TF-IDF features (word 1-2 grams and character 3-5 grams, so English and German both work)
feeding a logistic regression. Trained by evals/train_injection_classifier.py on dev data only;
the public test split is never used for training or for choosing the threshold.

Stored as plain JSON (vocabularies, IDF weights, coefficients, threshold), not pickle: loading a
pickle can execute arbitrary code, which a security gateway shouldn't ship. Loading rebuilds the
vectorizers from the stored numbers, and predicting is a dot product and a sigmoid.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from app.injection import normalize

MODEL_FILE = Path(__file__).parent / "injection_model.json"

# The two feature extractors. Shared with the training script so both sides always match.
VECTORIZER_PARAMS = {
    "words": {"analyzer": "word", "ngram_range": (1, 2), "min_df": 2, "sublinear_tf": True, "max_features": 20_000},
    "chars": {"analyzer": "char_wb", "ngram_range": (3, 5), "min_df": 2, "sublinear_tf": True, "max_features": 40_000},
}


def build_vectorizers() -> dict[str, TfidfVectorizer]:
    return {name: TfidfVectorizer(**params) for name, params in VECTORIZER_PARAMS.items()}


def features(vectorizers: dict[str, TfidfVectorizer], texts: list[str]):
    from scipy.sparse import hstack

    normalized = [normalize(t) for t in texts]
    return hstack([vectorizers[name].transform(normalized) for name in VECTORIZER_PARAMS]).tocsr()


#: The classifier reads at most this many characters from each end of a message. Its cost grows
#: with input size (43 ms for a 32 KB prompt), so an attacker sending maximum-size prompts could
#: burn the gateway's CPU. Attacks usually sit at the start or the end, and the rules still scan
#: the whole message; an attack buried mid-way through a long prompt reaches only the rules.
HEAD_TAIL_CHARS = 4_000


def head_and_tail(text: str, size: int = HEAD_TAIL_CHARS) -> str:
    return text if len(text) <= 2 * size else text[:size] + " " + text[-size:]


@dataclass
class InjectionClassifier:
    vectorizers: dict[str, TfidfVectorizer]
    coef: np.ndarray
    intercept: float
    threshold: float

    def probabilities(self, texts: list[str]) -> list[float]:
        texts = [head_and_tail(t) for t in texts]
        logits = features(self.vectorizers, texts) @ self.coef + self.intercept
        return [1 / (1 + math.exp(-float(z))) for z in np.ravel(logits)]

    @classmethod
    def load(cls, path: Path = MODEL_FILE) -> "InjectionClassifier":
        data = json.loads(path.read_text(encoding="utf-8"))
        vectorizers = {}
        for name, params in VECTORIZER_PARAMS.items():
            stored = data["vectorizers"][name]
            vectorizer = TfidfVectorizer(**{**params, "vocabulary": stored["vocabulary"]})
            vectorizer.idf_ = np.array(stored["idf"])
            vectorizers[name] = vectorizer
        return cls(
            vectorizers=vectorizers,
            coef=np.array(data["coef"]),
            intercept=float(data["intercept"]),
            threshold=float(data["threshold"]),
        )

    def save(self, path: Path, metadata: dict) -> None:
        payload = {
            "format": "bouncer-injection-classifier/1",
            "metadata": metadata,
            "threshold": self.threshold,
            "intercept": self.intercept,
            "coef": [round(float(c), 6) for c in self.coef],
            "vectorizers": {
                name: {
                    "vocabulary": {term: int(i) for term, i in v.vocabulary_.items()},
                    "idf": [round(float(x), 6) for x in v.idf_],
                }
                for name, v in self.vectorizers.items()
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
