"""Prompt-injection detection (DESIGN.md §4).

A heuristic layer, not a complete defense. It catches common, known attack phrasings; the
real defense is architectural (least privilege for the model's tools, never trusting model
output). Rules live in injection_rules.yaml as data.

How a request is scored:
1. Every message except `system` is scanned. System prompts come from the trusted developer
   and legitimately say things like "never reveal your instructions". Assistant messages ARE
   scanned: the client sends the whole history, so past "assistant" turns can be forged.
2. Each message is normalized into a detection copy (never forwarded): Unicode NFKC turns
   fullwidth letters into normal ones, invisible format characters (zero-width spaces, soft
   hyphens) are removed, common look-alike letters are mapped to Latin, and text is
   lowercased with whitespace collapsed.
3. Long base64 blocks are decoded once and scanned too.
4. Each rule that matches adds its weight once. At score >= threshold the request is blocked
   (or, in monitor mode, logged as "would have blocked").

Stated limitation: rules are English-only.
"""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.schemas import ChatMessage
from app.tokens import message_text

RULES_FILE = Path(__file__).parent / "injection_rules.yaml"

# Cyrillic and Greek letters that look like Latin ones ("Іgnоrе" with Cyrillic І and о).
_LOOKALIKES = str.maketrans(
    {
        "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
        "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ӏ": "l",
        "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o", "Р": "p", "С": "c",
        "Т": "t", "У": "y", "Х": "x", "І": "i", "Ј": "j", "Ѕ": "s",
        "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
        "υ": "u", "χ": "x", "Α": "a", "Β": "b", "Ε": "e", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n",
        "Ο": "o", "Ρ": "p", "Τ": "t", "Χ": "x", "Υ": "y", "Ζ": "z", "Η": "h",
    }
)
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w' ]+")
# Long enough that ordinary words and short tokens aren't decoded.
_BASE64_BLOCK = re.compile(r"[A-Za-z0-9+/]{24,8000}={0,2}")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = text.translate(_LOOKALIKES).lower()
    return _WHITESPACE.sub(" ", text).strip()


def to_words(normalized: str) -> str:
    """Punctuation becomes spaces, so "override: the rules" reads as "override the rules"."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", normalized)).strip()


def decoded_base64_blocks(text: str) -> list[str]:
    """Text hidden in base64. Only blocks that decode to readable UTF-8 are returned."""
    decoded = []
    for block in _BASE64_BLOCK.findall(text):
        try:
            raw = base64.b64decode(block + "=" * (-len(block) % 4), validate=True)
            plain = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if plain and sum(ch.isprintable() or ch.isspace() for ch in plain) / len(plain) > 0.9:
            decoded.append(plain)
    return decoded


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    weight: float = Field(gt=0)
    # Not "on": YAML 1.1 reads the key `on` as the boolean True.
    form: str = Field(pattern="^(words|text)$")
    description: str
    pattern: str
    examples: list[str] = []
    counterexamples: list[str] = []

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[Rule]


def load_rules(path: Path = RULES_FILE) -> list[Rule]:
    return RuleSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).rules


ENCODED_PAYLOAD_RULE = "encoded-payload"
_ENCODED_PAYLOAD_WEIGHT = 0.5


@dataclass(frozen=True)
class Verdict:
    score: float
    rules: list[str]
    blocked: bool


class InjectionDetector:
    def __init__(self, threshold: float = 1.0, rules: list[Rule] | None = None):
        self.threshold = threshold
        self.rules = rules if rules is not None else load_rules()
        self._compiled = [(rule, rule.compiled()) for rule in self.rules]

    def matching_rules(self, text: str) -> set[str]:
        normalized = normalize(text)
        forms = {"text": normalized, "words": to_words(normalized)}
        return {rule.id for rule, pattern in self._compiled if pattern.search(forms[rule.form])}

    def scan_text(self, text: str) -> set[str]:
        matched = self.matching_rules(text)
        for hidden in decoded_base64_blocks(text):
            if hidden_matches := self.matching_rules(hidden):
                # Encoding an instruction is itself a signal: honest users rarely do that.
                matched |= hidden_matches | {ENCODED_PAYLOAD_RULE}
        return matched

    def scan(self, messages: list[ChatMessage]) -> Verdict:
        matched: set[str] = set()
        for message in messages:
            if message.role != "system":
                # Each message on its own, so phrases can't be assembled across message boundaries.
                matched |= self.scan_text(message_text(message))
        weights = {rule.id: rule.weight for rule in self.rules} | {ENCODED_PAYLOAD_RULE: _ENCODED_PAYLOAD_WEIGHT}
        score = round(sum(weights[rule_id] for rule_id in matched), 2)
        return Verdict(score=score, rules=sorted(matched), blocked=score >= self.threshold)
