"""Measure injection detection on evals/injection_cases.jsonl.

Run from the repo root:  python -m evals.injection_eval

The cases are split in half. The rules in app/injection_rules.yaml were written while looking
only at the "dev" half, so dev numbers are optimistic by construction. The "test" half is the
honest number: rules must not be tuned to it. To keep it honest, a test-set miss is fixed only
by adding a dev case first and tuning against that.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.injection import InjectionDetector, Verdict
from app.schemas import ChatMessage

CASES_FILE = Path(__file__).parent / "injection_cases.jsonl"


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
    for line in cases_file.read_text(encoding="utf-8").splitlines():
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


def main() -> None:
    for split, report in sorted(evaluate().items()):
        print(f"[{split}] catch rate {report.caught}/{report.injections} ({report.catch_rate:.0%}), "
              f"false positives {report.false_positives}/{report.benign} ({report.false_positive_rate:.0%})")
        for kind, verdict, text in report.mistakes:
            print(f"    {kind:<14} score {verdict.score:<4} {verdict.rules}  {text[:70]!r}")


if __name__ == "__main__":
    main()
