"""Anthropic API provider with per-token cost measurement."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import anthropic

from .base import Completion, ProviderError

MODEL = "claude-opus-5"
FABLE_MODEL = "claude-fable-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MODEL_MAX_TOKENS = {MODEL: 16_000, FABLE_MODEL: 32_000}
_MODEL_PRICING = {MODEL: (5.0, 25.0), FABLE_MODEL: (10.0, 50.0)}


class APIProvider:
    """Complete prompts through the Anthropic Messages API."""

    name = "api"

    def __init__(
        self,
        *,
        output_schema: dict[str, Any] | None = None,
        model: str = MODEL,
        effort: str = "high",
        client: Any | None = None,
    ) -> None:
        self._output_schema = deepcopy(output_schema)
        self._model = model
        self._effort = effort
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
        """Stream one configured Anthropic response and return its final message."""

        self.preflight()
        output_config: dict[str, Any] = {"effort": self._effort}
        if self._output_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": deepcopy(self._output_schema),
            }
        arguments: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MODEL_MAX_TOKENS.get(self._model, 16_000),
            "messages": messages,
            "output_config": output_config,
            "system": system,
        }
        try:
            if self._model == FABLE_MODEL:
                arguments.update(
                    betas=[_FALLBACK_BETA],
                    fallbacks="default",
                )
                stream_method = self._client.beta.messages.stream
            else:
                arguments["thinking"] = {"type": "adaptive"}
                stream_method = self._client.messages.stream
            with stream_method(**arguments) as stream:
                response = stream.get_final_message()
        except Exception as error:
            raise ProviderError(f"Anthropic API completion failed: {error}") from error

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = _field(details, "category")
            explanation = _field(details, "explanation")
            message = "Anthropic API refused completion"
            if isinstance(category, str) and category:
                message += f" ({category})"
            if isinstance(explanation, str) and explanation:
                message += f": {explanation}"
            raise ProviderError(message)
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
        pricing = _MODEL_PRICING.get(self._model)
        cost_usd = None
        if pricing is not None:
            cost_usd = (
                input_tokens * pricing[0] + output_tokens * pricing[1]
            ) / 1_000_000
        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
