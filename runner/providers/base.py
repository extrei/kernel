"""Provider-neutral completion records and behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


JSON_PATCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "additionalProperties": False,
        "properties": {
            "op": {"enum": ["add", "replace", "remove", "test"]},
            "path": {"type": "string"},
            "value": {},
        },
        "required": ["op", "path"],
        "type": "object",
    },
}


class ProviderError(RuntimeError):
    """Raised when a completion provider is unavailable or returns bad data."""


@dataclass(frozen=True)
class Completion:
    """One provider result plus its measured usage."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


class Provider(Protocol):
    """The behavior required by one runner completion provider."""

    name: str

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> Completion:
        """Return one completion for a system prompt and message history."""

    def preflight(self) -> None:
        """Raise ProviderError when the provider cannot be used."""
