from decimal import Decimal
from pathlib import Path

import pytest

from app.models_registry import ModelRegistry
from tests.helpers import TEST_MODELS_FILE

PRODUCTION_MODELS_FILE = Path(__file__).parent.parent / "models.yaml"


def test_production_models_file_is_valid():
    registry = ModelRegistry.load(PRODUCTION_MODELS_FILE, {"ollama", "mock"})

    assert "mock" in registry.models


def test_unknown_upstream_fails_at_load(tmp_path):
    with pytest.raises(ValueError, match="unknown upstreams"):
        ModelRegistry.load(TEST_MODELS_FILE, {"some-other-upstream"})


def test_unknown_fields_are_rejected(tmp_path):
    bad = tmp_path / "models.yaml"
    bad.write_text(
        "models:\n  m:\n    upstream: test\n    upstream_model: m\n"
        "    input_price_per_1k: 0\n    output_price_per_1k: 0\n    typo_field: 1\n"
    )

    with pytest.raises(ValueError):
        ModelRegistry.load(bad, {"test"})


def test_cost_uses_per_1k_prices():
    route = ModelRegistry.load(TEST_MODELS_FILE, {"test"}).resolve("m")  # $1 in, $2 out per 1k

    assert route.cost(prompt_tokens=500, completion_tokens=250) == Decimal("1.0")
