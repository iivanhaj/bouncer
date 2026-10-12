"""Measure injection detection.

Run from the repo root:  python -m evals.injection_eval

Two datasets:
- PUBLIC: deepset/prompt-injections (evals/data/, English and German). Its "dev" split
  (deepset's train split) may be used to write rules; its "test" split is held out and is the
  headline number. Rules must never be tuned to it: a test-set miss is fixed only by finding
  the same pattern in dev data first.
- HAND_WRITTEN: evals/injection_cases.jsonl, written alongside the first version of the rules.
  Too easy to be a fair test (same author as the rules), so it is dev data now.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.injection import InjectionDetector, Verdict
from app.schemas import ChatMessage

CASES_FILE = Path(__file__).parent / "injection_cases.jsonl"
PUBLIC_FILE = Path(__file__).parent / "data" / "deepset_prompt_injections.jsonl"
DATASETS = {"public (deepset/prompt-injections)": PUBLIC_FILE, "hand-written": CASES_FILE}


@dataclass
class SplitReport:
    injections: int = 0
    caught: int = 0
    benign: int = 0
    false_positives: int = 0
    mistakes: list[tuple[str, Verdict, str]] = field(default_factory=list)

    @property
    def catch_rate(self) -> float:
        return self.caught / max(1, self.injections)

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / max(1, self.benign)


def evaluate(cases_file: Path = CASES_FILE, detector: InjectionDetector | None = None) -> dict[str, SplitReport]:
    detector = detector or InjectionDetector()
    reports: dict[str, SplitReport] = {}
    for line in cases_file.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        case = json.loads(line)
        report = reports.setdefault(case["split"], SplitReport())
        verdict = detector.scan([ChatMessage(role="user", content=case["text"])])
        if case["label"] == "injection":
            report.injections += 1
            report.caught += verdict.blocked
            if not verdict.blocked:
                report.mistakes.append(("missed", verdict, case["text"]))
        else:
            report.benign += 1
            report.false_positives += verdict.blocked
            if verdict.blocked:
                report.mistakes.append(("false positive", verdict, case["text"]))
    return reports


def detectors() -> dict[str, InjectionDetector]:
    """Both layers, so their contributions can be compared on the same data."""
    from app.injection_model import InjectionClassifier

    return {
        "rules only": InjectionDetector(),
        "rules + classifier": InjectionDetector(classifier=InjectionClassifier.load()),
    }


def main() -> None:
    for detector_name, detector in detectors().items():
        print(f"== {detector_name}")
        for name, path in DATASETS.items():
            print(name)
            for split, report in sorted(evaluate(path, detector).items(), reverse=True):
                label = f"{split} (held out)" if path == PUBLIC_FILE and split == "test" else split
                print(
                    f"  [{label}] catch rate {report.caught}/{report.injections} ({report.catch_rate:.0%}), "
                    f"false positives {report.false_positives}/{report.benign} ({report.false_positive_rate:.0%})"
                )


if __name__ == "__main__":
    main()
