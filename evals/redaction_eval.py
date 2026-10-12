"""Measure redaction quality on the hand-written cases in redaction_cases.jsonl.

Run from the repo root:  python -m evals.redaction_eval

Reports, per category and overall:
- catch rate (recall): of the items that should be redacted, how many were
- precision: of the items that were redacted, how many should have been
- false-positive prompts: prompts with nothing sensitive in which something was redacted anyway

Some cases are expected misses (formats out of scope, deliberate obfuscation); they are in the
set on purpose, so the catch rate is honest rather than 100% by construction.
"""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.redaction import find_spans

CASES_FILE = Path(__file__).parent / "redaction_cases.jsonl"


@dataclass
class Report:
    expected: Counter
    caught: Counter
    false_positives: Counter
    clean_prompts: int
    clean_prompts_flagged: int
    total_prompts: int

    @property
    def catch_rate(self) -> float:
        return sum(self.caught.values()) / max(1, sum(self.expected.values()))

    @property
    def precision(self) -> float:
        found = sum(self.caught.values()) + sum(self.false_positives.values())
        return sum(self.caught.values()) / max(1, found)

    @property
    def false_positive_prompt_rate(self) -> float:
        return self.clean_prompts_flagged / max(1, self.clean_prompts)


def evaluate(cases_file: Path = CASES_FILE) -> Report:
    expected, caught, false_positives = Counter(), Counter(), Counter()
    clean_prompts = clean_flagged = total = 0
    for line in cases_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        total += 1
        want = Counter(case["expected"])
        got = Counter(span.category for span in find_spans(case["text"]))
        for category in want | got:
            hits = min(want[category], got[category])
            caught[category] += hits
            false_positives[category] += got[category] - hits
        expected.update(want)
        if not want:
            clean_prompts += 1
            clean_flagged += bool(got)
    return Report(expected, caught, false_positives, clean_prompts, clean_flagged, total)


def main() -> None:
    report = evaluate()
    print(f"{report.total_prompts} prompts ({report.clean_prompts} with nothing sensitive)\n")
    print(f"{'category':<13} {'expected':>8} {'caught':>7} {'missed':>7} {'false +':>8}")
    for category in sorted(report.expected | report.false_positives):
        want, hit, fp = report.expected[category], report.caught[category], report.false_positives[category]
        print(f"{category:<13} {want:>8} {hit:>7} {want - hit:>7} {fp:>8}")
    print(f"\ncatch rate (recall):    {report.catch_rate:.1%}")
    print(f"precision:              {report.precision:.1%}")
    print(f"false-positive prompts: {report.clean_prompts_flagged}/{report.clean_prompts} "
          f"({report.false_positive_prompt_rate:.1%})")


if __name__ == "__main__":
    main()
