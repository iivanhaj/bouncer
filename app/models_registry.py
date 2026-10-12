from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.errors import GatewayError


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream: str
    upstream_model: str
    input_price_per_1k: Decimal = Field(ge=0)
    output_price_per_1k: Decimal = Field(ge=0)

    def cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return (
            Decimal(prompt_tokens) * self.input_price_per_1k + Decimal(completion_tokens) * self.output_price_per_1k
        ) / 1000


class ModelRegistry(BaseModel):
    """The model allowlist and its routing, loaded once at startup from models.yaml."""

    model_config = ConfigDict(extra="forbid")

    models: dict[str, ModelRoute]

    @classmethod
    def load(cls, path: Path, known_upstreams: set[str]) -> "ModelRegistry":
        registry = cls.model_validate(yaml.safe_load(path.read_text()))
        # Fail at startup, not on the first request, if a model points at an upstream that doesn't exist.
        unknown = {route.upstream for route in registry.models.values()} - known_upstreams
        if unknown:
            raise ValueError(f"{path} refers to unknown upstreams {sorted(unknown)}; known: {sorted(known_upstreams)}")
        return registry

    def resolve(self, name: str) -> ModelRoute:
        route = self.models.get(name)
        if route is None:
            raise GatewayError(
                404,
                f"The model '{name}' does not exist or is not available",
                "invalid_request_error",
                "model_not_found",
            )
        return route
