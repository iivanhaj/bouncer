"""API key format: bnc_<43 base62 chars>_<6 hex checksum>.

- "bnc_" makes leaked keys recognisable to secret scanners (like GitHub's).
- The body is 32 random bytes (256 bits), so keys can't be guessed.
- The checksum lets the gateway reject typos and junk keys without a database query.
"""

import hashlib
import re
import secrets
import string

PREFIX = "bnc_"
_ALPHABET = string.digits + string.ascii_letters  # base62: no "_" or "-", so the separators stay unambiguous
_BODY_LENGTH = 43  # 32 bytes need at most 43 base62 characters
_CHECKSUM_LENGTH = 6
_KEY_PATTERN = re.compile(rf"^{PREFIX}([0-9A-Za-z]{{{_BODY_LENGTH}}})_([0-9a-f]{{{_CHECKSUM_LENGTH}}})$")
DISPLAY_PREFIX_LENGTH = len(PREFIX) + 8


def _base62(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    chars = []
    while number:
        number, remainder = divmod(number, 62)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars)).rjust(_BODY_LENGTH, "0")


def _checksum(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:_CHECKSUM_LENGTH]


def generate_key() -> str:
    body = _base62(secrets.token_bytes(32))
    return f"{PREFIX}{body}_{_checksum(body)}"


def is_well_formed(key: str) -> bool:
    """Format and checksum check. Cheap, and needs no database."""
    match = _KEY_PATTERN.match(key)
    return match is not None and _checksum(match.group(1)) == match.group(2)


def hash_key(key: str) -> str:
    """SHA-256, not bcrypt: keys are 256-bit random, so a slow hash would add latency and no security."""
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    return key[:DISPLAY_PREFIX_LENGTH]
