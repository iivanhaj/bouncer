"""All prompt-injection evaluation data, loaded in one shape.

Each dataset row has a split: "dev" rows may be used for training and choosing thresholds,
"test" rows are only ever scored. Gandalf has no dev rows: it is real attacks that no model is
ever trained on.
"""

import json
from dataclasses import dataclass
from pathlib import Path

EVALS = Path(__file__).parent
FILES = {
    "deepset": EVALS / "data" / "deepset_prompt_injections.jsonl",
    "jailbreak": EVALS / "data" / "jailbreak_classification.jsonl",
    "spml": EVALS / "data" / "spml.jsonl",
    "gandalf": EVALS / "data" / "gandalf.jsonl",
    # Written alongside the first rules; both halves are dev data.
    "hand_written": EVALS / "injection_cases.jsonl",
}
# Datasets with a held-out test split that includes benign prompts (so false positives can be measured).
SCORED = ["deepset", "jailbreak", "spml"]


@dataclass(frozen=True)
class Case:
    dataset: str
    split: str
    label: int  # 1 = injection
    text: str


def load(name: str) -> list[Case]:
    cases = []
    for line in FILES[name].read_text(encoding="utf-8").split("\n"):
        if line.strip():
            row = json.loads(line)
            split = "dev" if name == "hand_written" else row["split"]
            cases.append(Case(name, split, 1 if row["label"] == "injection" else 0, row["text"]))
    return cases


def load_all() -> dict[str, list[Case]]:
    return {name: load(name) for name in FILES}
