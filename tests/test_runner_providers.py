from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runner.providers import APIProvider, ClaudeCodeProvider, ProviderError


class _Stream:
    def __init__(self, response: object) -> None:
        self.response = response
        self.final_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get_final_message(self) -> object:
        self.final_calls += 1
        return self.response


class _Messages:
    def __init__(self, response: object) -> None:
        self.stream_object = _Stream(response)
        self.arguments: dict[str, object] | None = None

    def stream(self, **arguments: object) -> _Stream:
        self.arguments = arguments
        return self.stream_object


class RunnerProviderTests(unittest.TestCase):
    def test_api_provider_uses_fixed_model_adaptive_thinking_and_streaming(self) -> None:
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="[]")],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )
        messages = _Messages(response)
        client = SimpleNamespace(messages=messages)
        provider = APIProvider(output_schema={"type": "array"}, client=client)

        completion = provider.complete(
            system="system",
            messages=[{"role": "user", "content": "task"}],
        )

        self.assertEqual(completion.text, "[]")
        self.assertEqual(completion.input_tokens, 100)
        self.assertEqual(completion.output_tokens, 20)
        self.assertAlmostEqual(completion.cost_usd, 0.001)
        self.assertEqual(messages.stream_object.final_calls, 1)
        arguments = messages.arguments
        self.assertEqual(arguments["model"], "claude-opus-5")
        self.assertEqual(arguments["max_tokens"], 16_000)
        self.assertEqual(arguments["thinking"], {"type": "adaptive"})
        self.assertEqual(arguments["output_config"]["effort"], "high")
        self.assertEqual(
            arguments["output_config"]["format"],
            {"type": "json_schema", "schema": {"type": "array"}},
        )
        self.assertNotIn("budget_tokens", repr(arguments))

    def test_api_preflight_constructs_a_bare_client(self) -> None:
        client = object()
        with patch("runner.providers.api.anthropic.Anthropic", return_value=client) as factory:
            provider = APIProvider()
            provider.preflight()
            provider.preflight()

        factory.assert_called_once_with()

    def test_fable_provider_uses_beta_fallback_without_thinking(self) -> None:
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="{}")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )
        messages = _Messages(response)
        client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
        provider = APIProvider(
            output_schema={"type": "object"},
            model="claude-fable-5",
            effort="xhigh",
            client=client,
        )

        completion = provider.complete(
            system="system",
            messages=[{"role": "user", "content": "task"}],
        )

        self.assertEqual(completion.text, "{}")
        self.assertAlmostEqual(completion.cost_usd, 0.002)
        arguments = messages.arguments
        self.assertEqual(arguments["model"], "claude-fable-5")
        self.assertEqual(arguments["max_tokens"], 32_000)
        self.assertEqual(arguments["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(arguments["fallbacks"], "default")
        self.assertEqual(arguments["output_config"]["effort"], "xhigh")
        self.assertNotIn("thinking", arguments)
        self.assertNotIn("budget_tokens", repr(arguments))

    def test_fable_refusal_surfaces_category_before_content(self) -> None:
        response = SimpleNamespace(
            content=[],
            stop_details=SimpleNamespace(
                category="reasoning_extraction",
                explanation="The request sought hidden reasoning.",
            ),
            stop_reason="refusal",
        )
        messages = _Messages(response)
        client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
        provider = APIProvider(
            model="claude-fable-5",
            effort="xhigh",
            client=client,
        )

        with self.assertRaisesRegex(ProviderError, "reasoning_extraction"):
            provider.complete(
                system="system",
                messages=[{"role": "user", "content": "task"}],
            )

    def test_claude_code_parses_recorded_envelope_and_restricts_tools(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run_process(arguments: list[str], **keywords: object):
            calls.append((arguments, keywords))
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=self._fixture("claude_code_success.json"),
                stderr="",
            )

        provider = ClaudeCodeProvider(".", run_process=run_process)
        completion = provider.complete(
            system="system",
            messages=[{"role": "user", "content": "patch"}],
        )

        self.assertEqual(completion.input_tokens, 24)
        self.assertEqual(completion.output_tokens, 7)
        self.assertEqual(completion.cost_usd, 0.0123)
        arguments, keywords = calls[0]
        self.assertIn("--output-format", arguments)
        self.assertIn("--model", arguments)
        self.assertIn("--allowedTools", arguments)
        self.assertIn("--tools", arguments)
        self.assertIn("--append-system-prompt", arguments)
        self.assertEqual(arguments[arguments.index("--tools") + 1], "")
        self.assertEqual(
            arguments[arguments.index("--allowedTools") + 1],
            "mcp__kernel__get_view,mcp__kernel__submit_patch",
        )
        self.assertNotIn("--allow-dangerously-skip-permissions", arguments)
        self.assertNotIn("--dangerously-skip-permissions", arguments)
        self.assertEqual(keywords["check"], False)
        self.assertEqual(keywords["capture_output"], True)

    def test_claude_code_preflight_surfaces_login_failure(self) -> None:
        def run_process(arguments: list[str], **keywords: object):
            return subprocess.CompletedProcess(
                arguments,
                1,
                stdout=self._fixture("claude_code_error.json"),
                stderr="",
            )

        provider = ClaudeCodeProvider(".", run_process=run_process)

        with self.assertRaisesRegex(ProviderError, "Not logged in.*run /login"):
            provider.preflight()

    @staticmethod
    def _fixture(name: str) -> str:
        return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
