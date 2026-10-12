import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.redaction import DETECTORS, Redactor, find_spans, luhn_valid, verhoeff_valid
from app.schemas import ChatMessage, TextPart
from tests.helpers import UPSTREAM_URL, create_key, fetch_rows, make_settings

COMPLETION = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}


def categories(text: str) -> list[str]:
    return [span.category for span in find_spans(text)]


def redact(text: str) -> str:
    return Redactor().redact_text(text)


# --- Checksums -----------------------------------------------------------------


def test_luhn():
    assert luhn_valid("4111111111111111")  # Visa test number
    assert not luhn_valid("4111111111111112")


def test_verhoeff():
    assert verhoeff_valid("234123412346")
    assert not verhoeff_valid("234123412345")


# --- Each category: things that must be caught, and look-alikes that must not ---------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Emails
        ("mail jane.doe+work@example.co.uk now", ["EMAIL"]),
        ("not an email: jane@localhost", []),
        # Credit cards (Luhn-valid test numbers)
        ("card 4111 1111 1111 1111 exp 12/29", ["CREDIT_CARD"]),
        ("card 4111-1111-1111-1111", ["CREDIT_CARD"]),
        ("card 378282246310005", ["CREDIT_CARD"]),  # Amex, 15 digits
        ("order 4111111111111112 shipped", []),  # fails Luhn
        # US SSN
        ("ssn 123-45-6789", ["NATIONAL_ID"]),
        ("ssn 000-12-3456", []),  # invalid area number
        ("ssn 666-12-3456", []),
        ("id 123456789", []),  # no dashes: too ambiguous
        # Aadhaar (Verhoeff-valid)
        ("aadhaar 2341 2341 2346", ["NATIONAL_ID"]),
        ("aadhaar 2341 2341 2345", []),  # fails Verhoeff
        # Phones
        ("call +1 415 555 0132", ["PHONE"]),
        ("call +919876543210", ["PHONE"]),
        ("call (415) 555-0132", ["PHONE"]),
        ("call 415-555-0132", ["PHONE"]),
        ("call 98765 43210", ["PHONE"]),
        ("order id 4155550132", []),  # no separators, no +
        ("on 2026-10-12", []),  # dates aren't phones
        # IPs
        ("server 192.168.1.20 is down", ["IP_ADDRESS"]),
        ("version 999.1.2.3", []),  # octet > 255
        # Secrets
        ("key AKIAIOSFODNN7EXAMPLE", ["SECRET"]),
        ("token ghp_" + "a" * 36, ["SECRET"]),
        ("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx", ["SECRET"]),
        ("anthropic sk-ant-api03-abcdefghijklmnopqrstu", ["SECRET"]),
        ("slack xoxb-1234567890-abcdefghij", ["SECRET"]),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc123", ["SECRET"]),
        ("my key bnc_" + "A" * 43 + "_0a1b2c", ["SECRET"]),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----", ["SECRET"]),
        ("a uuid 123e4567-e89b-12d3-a456-426614174000 is not a secret", []),
        ("sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", []),
        # Nothing to redact
        ("What is the capital of France?", []),
    ],
)
def test_detection(text, expected):
    assert categories(text) == expected


def test_overlaps_are_redacted_once_by_priority():
    # 16 Luhn-valid digits after "+": both the phone and card patterns match. Card wins.
    assert categories("pay +4111111111111111") == ["CREDIT_CARD"]


def test_multiple_items_in_one_text():
    text = "Email bob@example.com or call 415-555-0132, card 4111 1111 1111 1111."

    assert redact(text) == "Email [EMAIL_1] or call [PHONE_1], card [CREDIT_CARD_1]."


# --- Placeholders ----------------------------------------------------------------


def test_same_value_gets_same_placeholder_across_messages():
    redactor = Redactor()
    messages = [
        ChatMessage(role="user", content="Write to ann@example.com"),
        ChatMessage(role="assistant", content="Sure, what should I tell ann@example.com?"),
        ChatMessage(role="user", content="Also cc bob@example.com"),
    ]

    redacted = redactor.redact_messages(messages)

    assert [m.content for m in redacted] == [
        "Write to [EMAIL_1]",
        "Sure, what should I tell [EMAIL_1]?",
        "Also cc [EMAIL_2]",
    ]
    assert redactor.counts == {"EMAIL": 2}


def test_all_roles_and_text_parts_are_redacted():
    redactor = Redactor()
    messages = [
        ChatMessage(role="system", content="Admin email is root@example.com"),
        ChatMessage(role="user", content=[TextPart(type="text", text="my ip 10.0.0.1")]),
    ]

    redacted = redactor.redact_messages(messages)

    assert redacted[0].content == "Admin email is [EMAIL_1]"
    assert redacted[1].content[0].text == "my ip [IP_ADDRESS_1]"
    assert redactor.counts == {"EMAIL": 1, "IP_ADDRESS": 1}


def test_original_messages_are_not_modified():
    message = ChatMessage(role="user", content="x@example.com")

    Redactor().redact_messages([message])

    assert message.content == "x@example.com"


# --- ReDoS: no pattern may take more than linear-ish time ----------------------------

ADVERSARIAL_INPUTS = {
    "letters": "a" * 100_000,
    "digits": "1" * 100_000,
    "spaced digits": "1 " * 50_000,
    "dashed digits": "1-" * 50_000,
    "almost email": "a." * 50_000 + "@",
    "many ats": "a@" * 50_000,
    "dotted labels": "a@" + "b." * 50_000,
    "almost ip": "1." * 50_000,
    "almost jwt": "eyJ" + "a" * 50_000 + ".eyJ" + "b" * 49_000,
    "pem headers without end": "-----BEGIN RSA PRIVATE KEY-----" * 3_000,
    "plus digits": "+1" * 50_000,
}


@pytest.mark.parametrize("name", ADVERSARIAL_INPUTS)
def test_patterns_stay_fast_on_adversarial_input(name):
    text = ADVERSARIAL_INPUTS[name]
    for detector in DETECTORS:
        start = time.perf_counter()
        for _ in detector.pattern.finditer(text):
            pass
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"{detector.category} pattern {detector.pattern.pattern[:40]!r} took {elapsed:.2f}s on {name}"


@pytest.mark.parametrize("name", ADVERSARIAL_INPUTS)
def test_full_redaction_stays_fast_on_adversarial_input(name):
    # The whole path: PEM search (not a single regex), overlap resolution and replacement.
    # Inputs like "1." * 50_000 produce thousands of matches, which exposed quadratic steps.
    start = time.perf_counter()
    Redactor().redact_text(ADVERSARIAL_INPUTS[name])
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"redaction took {elapsed:.2f}s on {name}"


# --- Through the gateway -------------------------------------------------------------


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))


def test_upstream_receives_redacted_prompt_and_counts_are_logged(upstream):
    secret_prompt = "Email jane@example.com, card 4111 1111 1111 1111, again jane@example.com"
    with TestClient(create_app(make_settings())) as client:
        key = create_key(client)["key"]
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": secret_prompt}]},
            headers={"Authorization": f"Bearer {key}"},
        )

    assert response.status_code == 200
    sent = json.loads(upstream.calls.last.request.content)
    assert sent["messages"][0]["content"] == "Email [EMAIL_1], card [CREDIT_CARD_1], again [EMAIL_1]"
    [row] = fetch_rows("SELECT redaction_counts FROM requests")
    assert json.loads(row["redaction_counts"]) == {"EMAIL": 1, "CREDIT_CARD": 1}


def test_nothing_to_redact_logs_null(upstream):
    with TestClient(create_app(make_settings())) as client:
        key = create_key(client)["key"]
        client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "What is 2 + 2?"}]},
            headers={"Authorization": f"Bearer {key}"},
        )

    assert fetch_rows("SELECT redaction_counts FROM requests")[0]["redaction_counts"] is None
