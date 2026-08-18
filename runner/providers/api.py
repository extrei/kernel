"""Anthropic API provider with per-token cost measurement."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import anthropic

from .base import Completion, ProviderError

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000
_INPUT_USD_PER_MILLION = 5.0
_OUTPUT_USD_PER_MILLION = 25.0


class APIProvider:
    """Complete prompts through the Anthropic Messages API."""

    name = "api"

    def __init__(
        self,
        *,
        output_schema: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        self._output_schema = deepcopy(output_schema)
        self._client = client

    def preflight(self) -> None:
        """Resolve the SDK credential chain without prompting for a key."""

        if self._client is not None:
            return
        try:
            self._client = anthropic.Anthropic()
        except Exception as error:
            raise ProviderError(f"Anthropic API is unavailable: {error}") from error

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> Completion:
        """Stream one high-effort Opus response and return its final message."""

        self.preflight()
        output_config: dict[str, Any] = {"effort": "high"}
        if self._output_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": deepcopy(self._output_schema),
            }
        try:
            with self._client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=messages,
                output_config=output_config,
                system=system,
                thinking={"type": "adaptive"},
            ) as stream:
                response = stream.get_final_message()
        except Exception as error:
            raise ProviderError(f"Anthropic API completion failed: {error}") from error

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        if not text:
            raise ProviderError("Anthropic API returned no text content")
        try:
            input_tokens = int(response.usage.input_tokens)
            output_tokens = int(response.usage.output_tokens)
        except (AttributeError, TypeError, ValueError) as error:
            raise ProviderError("Anthropic API returned invalid token usage") from error
        cost_usd = (
            input_tokens * _INPUT_USD_PER_MILLION
            + output_tokens * _OUTPUT_USD_PER_MILLION
        ) / 1_000_000
        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
