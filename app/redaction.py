"""Redaction of personal data and secrets before a prompt leaves the gateway (DESIGN.md §3).

Scope: catches ACCIDENTAL leaks by honest users ("a seatbelt, not a vault"). Someone who
deliberately writes "john at gmail dot com" owns that data and chose to send it.

How it works:
1. Every detector finds candidate spans. Detectors with checksums (cards, Aadhaar) reject
   random numbers that only look right.
2. Overlapping spans are resolved by a fixed priority, so one piece of text is redacted once.
3. Spans are replaced from the end of the string backwards, so earlier positions don't shift.
4. The same value gets the same placeholder in every message ([EMAIL_1] twice, not
   [EMAIL_1] then [EMAIL_2]), so the model can still follow the conversation.

ReDoS: Python's `re` backtracks, so a pattern with nested, overlapping repetition can take
exponential time on a crafted input and freeze the event loop for every user. Every pattern
here uses bounded repetition, and every repeated group starts or ends with a character the
group's body can't match, so there is only one way to split the text.
tests/test_redaction.py feeds each pattern 100 KB of adversarial input to prove it.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.schemas import ChatMessage, TextPart

# --- Checksums -----------------------------------------------------------------


def luhn_valid(digits: str) -> bool:
    """Credit card checksum: rejects about 90% of random digit strings."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(digits: str) -> bool:
    """Aadhaar (Indian national ID) checksum."""
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


# --- Detectors -------------------------------------------------------------------


@dataclass(frozen=True)
class Detector:
    category: str
    # Lower number wins when two detections overlap.
    priority: int
    pattern: re.Pattern[str]
    # Extra check on the matched text, e.g. a checksum. None means the pattern is enough.
    validate: Callable[[str], bool] | None = None


def _card_valid(text: str) -> bool:
    digits = _digits(text)
    return 13 <= len(digits) <= 19 and luhn_valid(digits)


def _aadhaar_valid(text: str) -> bool:
    digits = _digits(text)
    return len(digits) == 12 and verhoeff_valid(digits)


def _ipv4_valid(text: str) -> bool:
    return all(int(octet) <= 255 for octet in text.split("."))


DETECTORS: list[Detector] = [
    # Secrets: known formats only. "Random-looking string" detection would flag every hash and UUID.
    # (PEM private keys are found by _pem_spans below, not a regex.)
    Detector("SECRET", 0, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),  # AWS access key id
    Detector("SECRET", 0, re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),  # GitHub token
    Detector("SECRET", 0, re.compile(r"\bsk-[A-Za-z0-9_-]{20,300}")),  # OpenAI / Anthropic style
    Detector("SECRET", 0, re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,300}")),  # Slack token
    Detector("SECRET", 0, re.compile(r"\beyJ[A-Za-z0-9_-]{5,4000}\.eyJ[A-Za-z0-9_-]{5,8000}\.[A-Za-z0-9_-]{0,2000}")),  # JWT
    Detector("SECRET", 0, re.compile(r"\bbnc_[0-9A-Za-z]{43}_[0-9a-f]{6}\b")),  # this gateway's own keys
    # 13-19 digits, optionally separated by single spaces or dashes, then the Luhn check.
    # Not glued to a letter, digit or dash: a checksum alone passes ~10% of random numbers,
    # so context rules out digit runs inside UUIDs, hashes and other identifiers.
    Detector("CREDIT_CARD", 1, re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])"), _card_valid),
    # US SSN: dashes required (bare 9-digit numbers are too often something else), invalid ranges excluded.
    Detector("NATIONAL_ID", 2, re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    # Aadhaar: 12 digits, first digit 2-9, optionally grouped 4-4-4, then the Verhoeff check.
    Detector("NATIONAL_ID", 2, re.compile(r"(?<![\w-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\w-])"), _aadhaar_valid),
    Detector("EMAIL", 3, re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}\b")),
    # Phones need a "+" country code or separators, so order numbers and dates aren't flagged.
    Detector("PHONE", 4, re.compile(r"(?<![\w+])\+\d{1,3}(?:[ -]?\d){6,12}\b")),  # international (E.164-ish)
    Detector("PHONE", 4, re.compile(r"(?<!\w)\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),  # US: 555-123-4567, (555) 123-4567
    Detector("PHONE", 4, re.compile(r"(?<![\w+])[6-9]\d{4}[ -]\d{5}\b")),  # India mobile: 98765 43210
    Detector("IP_ADDRESS", 5, re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), _ipv4_valid),
]


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    category: str
    priority: int


_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]{0,40}PRIVATE KEY-----")


def _pem_spans(text: str) -> list[Span]:
    """PEM private key blocks, from BEGIN to the next END.

    Not a single regex on purpose: "BEGIN, anything, END" as a regex rescans the rest of the
    text from every BEGIN, which is quadratic on text with many BEGINs and no END (measured:
    0.5s on 80 KB). Here each END search starts where the last one stopped, and the first
    BEGIN with no END after it ends the search: linear time.
    """
    spans = []
    position = 0
    while begin := _PEM_BEGIN.search(text, position):
        end = _PEM_END.search(text, begin.end())
        if end is None:
            break
        spans.append(Span(begin.start(), end.end(), "SECRET", 0))
        position = end.end()
    return spans


def find_spans(text: str) -> list[Span]:
    """All detections, with overlaps resolved: higher priority wins, then the longer match."""
    candidates = _pem_spans(text) + [
        Span(m.start(), m.end(), d.category, d.priority)
        for d in DETECTORS
        for m in d.pattern.finditer(text)
        if d.validate is None or d.validate(m.group())
    ]
    candidates.sort(key=lambda s: (s.priority, -(s.end - s.start), s.start))
    # One byte per character, set once that character is redacted. Checking a candidate costs
    # its own length, not the number of spans already chosen: comparing against every chosen
    # span was quadratic (measured: 5.3s on "1." * 50_000, which holds ~12,500 IP addresses).
    taken = bytearray(len(text))
    chosen: list[Span] = []
    for span in candidates:
        if taken.find(1, span.start, span.end) == -1:
            taken[span.start : span.end] = b"\x01" * (span.end - span.start)
            chosen.append(span)
    return sorted(chosen, key=lambda s: s.start)


# --- Redacting a request -----------------------------------------------------------


class Redactor:
    """Redacts one request. Holds the value -> placeholder mapping for that request only;
    it is never stored or logged."""

    def __init__(self) -> None:
        self._placeholders: dict[tuple[str, str], str] = {}
        self.counts: dict[str, int] = {}

    def _placeholder(self, category: str, value: str) -> str:
        key = (category, value)
        if key not in self._placeholders:
            self.counts[category] = self.counts.get(category, 0) + 1
            self._placeholders[key] = f"[{category}_{self.counts[category]}]"
        return self._placeholders[key]

    def redact_text(self, text: str) -> str:
        # Build the result in one pass from the spans (in reading order, so placeholders are
        # numbered in the order they appear). Slicing the whole string once per span would
        # copy the text once per span: quadratic with thousands of spans.
        pieces = []
        position = 0
        for span in find_spans(text):
            pieces.append(text[position : span.start])
            pieces.append(self._placeholder(span.category, text[span.start : span.end]))
            position = span.end
        pieces.append(text[position:])
        return "".join(pieces)

    def redact_messages(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Every role is redacted: system prompts and history can contain personal data too."""
        redacted = []
        for message in messages:
            if isinstance(message.content, str):
                content: str | list[TextPart] = self.redact_text(message.content)
            else:
                content = [part.model_copy(update={"text": self.redact_text(part.text)}) for part in message.content]
            redacted.append(message.model_copy(update={"content": content}))
        return redacted
