from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kernel import BlueprintError, blueprint, entries, initialize
from runner.architect import compose, install
from runner.providers import Completion


class _FakeProvider:
    name = "api"

    def __init__(self, texts: list[str]) -> None:
        self.texts = iter(texts)
        self.calls: list[dict[str, object]] = []
        self.preflight_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1

    def complete(self, **arguments: object) -> Completion:
        self.calls.append(arguments)
        return Completion(next(self.texts), 10, 5, 0.000175)


class RunnerArchitectTests(unittest.TestCase):
    def test_malformed_blueprint_retries_then_raises_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            provider = _FakeProvider(["not-json", "[]"])

            with patch("runner.architect.APIProvider", return_value=provider):
                with self.assertRaises(BlueprintError):
                    install(project, "task", task_id="architect", attempts=2)

            self.assertEqual(len(provider.calls), 2)
            self.assertIn("not valid JSON", provider.calls[1]["messages"][0]["content"])
            self.assertIsNone(blueprint(project))
            self.assertEqual(entries(project), [])

    def test_contract_path_absent_from_schema_installs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            document = {
                "version": 2,
                "schema": {
                    "additionalProperties": False,
                    "properties": {"allowed": {"type": "string"}},
                    "type": "object",
                },
                "contracts": {
                    "version": 2,
                    "actors": {"worker": {"read": [], "write": ["/missing"]}},
                },
            }
            provider = _FakeProvider([self._json(document)])

            with patch("runner.architect.APIProvider", return_value=provider):
                with self.assertRaisesRegex(BlueprintError, "absent from schema"):
                    install(project, "task", task_id="architect", attempts=1)

            self.assertIsNone(blueprint(project))
            self.assertEqual(entries(project), [])

    def test_dry_composition_uses_api_and_never_installs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            document = self._valid_blueprint()
            provider = _FakeProvider([self._json(document)])

            with patch("runner.architect.APIProvider", return_value=provider) as factory:
                result = compose(project, "task", task_id="architect")

            self.assertEqual(result, document)
            self.assertEqual(provider.preflight_calls, 1)
            self.assertIn("output_schema", factory.call_args.kwargs)
            self.assertIsNone(blueprint(project))
            self.assertEqual(entries(project), [])

    @staticmethod
    def _valid_blueprint() -> dict[str, object]:
        return {
            "version": 2,
            "schema": None,
            "contracts": {
                "version": 2,
                "actors": {"worker": {"read": [], "write": ["/result"]}},
            },
        }

    @staticmethod
    def _json(value: object) -> str:
        import json

        return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
