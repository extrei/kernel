from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest

from runner.providers import CodexProvider, ProviderError


class RunnerCodexProviderTests(unittest.TestCase):
    def test_reads_last_message_and_enforces_read_only_schema_bound_argv(self) -> None:
        observed: dict[str, object] = {}
        patch = '[{"op":"add","path":"/status","value":"done"}]'

        def run_process(arguments: list[str], **keywords: object):
            schema_path = Path(arguments[arguments.index("--output-schema") + 1])
            output_path = Path(
                arguments[arguments.index("--output-last-message") + 1]
            )
            observed.update(
                arguments=arguments,
                keywords=keywords,
                output_path=output_path,
                schema=json.loads(schema_path.read_text(encoding="utf-8")),
                schema_path=schema_path,
            )
            output_path.write_text(patch, encoding="utf-8")
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=(
                    '{"type":"item.completed","item":{"text":"not the patch"}}\n'
                    '{"type":"turn.completed","usage":'
                    '{"input_tokens":31,"cached_input_tokens":12,'
                    '"output_tokens":9,"reasoning_output_tokens":4}}\n'
                ),
                stderr="",
            )

        provider = CodexProvider(".", run_process=run_process)
        completion = provider.complete(
            system="return a patch",
            messages=[{"role": "user", "content": "task"}],
        )

        self.assertEqual(completion.text, patch)
        self.assertEqual(completion.input_tokens, 31)
        self.assertEqual(completion.output_tokens, 9)
        self.assertIsNone(completion.cost_usd)
        arguments = observed["arguments"]
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", arguments)
        self.assertIn("--output-schema", arguments)
        self.assertIn("--output-last-message", arguments)
        self.assertIn("--json", arguments)
        self.assertIn('model_reasoning_effort="max"', arguments)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", arguments)
        self.assertNotIn("--dangerously-bypass-hook-trust", arguments)
        self.assertEqual(observed["schema"]["type"], "array")
        self.assertEqual(observed["keywords"]["check"], False)
        self.assertEqual(observed["keywords"]["capture_output"], True)
        self.assertFalse(observed["schema_path"].exists())
        self.assertFalse(observed["output_path"].exists())

    def test_missing_or_empty_output_is_an_error_and_temps_are_removed(self) -> None:
        for create_empty in (False, True):
            with self.subTest(create_empty=create_empty):
                observed: dict[str, Path] = {}

                def run_process(arguments: list[str], **keywords: object):
                    observed["schema"] = Path(
                        arguments[arguments.index("--output-schema") + 1]
                    )
                    observed["output"] = Path(
                        arguments[arguments.index("--output-last-message") + 1]
                    )
                    if create_empty:
                        observed["output"].write_text("", encoding="utf-8")
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout="", stderr=""
                    )

                provider = CodexProvider(".", run_process=run_process)
                with self.assertRaisesRegex(ProviderError, "final message"):
                    provider.complete(system="system", messages=[])

                self.assertFalse(observed["schema"].exists())
                self.assertFalse(observed["output"].exists())

    def test_malformed_jsonl_usage_falls_back_to_unknown_usage(self) -> None:
        def run_process(arguments: list[str], **keywords: object):
            output_path = Path(
                arguments[arguments.index("--output-last-message") + 1]
            )
            output_path.write_text("[]", encoding="utf-8")
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=(
                    "not json\n"
                    '{"type":"turn.completed","usage":'
                    '{"input_tokens":"unknown","output_tokens":null}}\n'
                ),
                stderr="",
            )

        completion = CodexProvider(".", run_process=run_process).complete(
            system="system",
            messages=[],
        )

        self.assertEqual(completion.text, "[]")
        self.assertEqual(completion.input_tokens, 0)
        self.assertEqual(completion.output_tokens, 0)
        self.assertIsNone(completion.cost_usd)

    def test_nonzero_exit_surfaces_stderr_and_removes_temp_files(self) -> None:
        observed: dict[str, Path] = {}

        def run_process(arguments: list[str], **keywords: object):
            observed["schema"] = Path(
                arguments[arguments.index("--output-schema") + 1]
            )
            observed["output"] = Path(
                arguments[arguments.index("--output-last-message") + 1]
            )
            return subprocess.CompletedProcess(
                arguments,
                2,
                stdout="",
                stderr="Not logged in; run codex login",
            )

        provider = CodexProvider(".", run_process=run_process)
        with self.assertRaisesRegex(ProviderError, "Not logged in.*codex login"):
            provider.complete(system="system", messages=[])

        self.assertFalse(observed["schema"].exists())
        self.assertFalse(observed["output"].exists())


if __name__ == "__main__":
    unittest.main()
