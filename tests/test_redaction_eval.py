"""Guards the measured redaction quality (eval/redaction_cases.jsonl) against regressions."""

import json
from collections import Counter

import pytest

from app.redaction import find_spans
from evals.redaction_eval import CASES_FILE, evaluate

CASES = [json.loads(line) for line in CASES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_overall_quality_does_not_regress():
    report = evaluate()

    assert report.catch_rate >= 0.92
    assert report.false_positive_prompt_rate <= 0.03


@pytest.mark.parametrize(
    "case",
    [
        c
        for c in CASES
        if "expected miss" not in c.get("note", "") and "accepted false positive" not in c.get("note", "")
    ],
    ids=lambda c: c["text"][:50],
)
def test_each_case_matches_exactly(case):
    assert Counter(span.category for span in find_spans(case["text"])) == Counter(case["expected"])
