from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kernel import initialize, set_blueprint
from runner.cli import _worker_provider, main
from runner.providers import Completion


class RunnerCLITests(unittest.TestCase):
    def test_dry_run_uses_architect_api_path_and_installs_nothing(self) -> None:
        document = {
            "version": 3,
            "schema": None,
            "contracts": {"version": 2, "actors": {"worker": {}}},
        }
        output = io.StringIO()

        with (
            patch("runner.cli.compose", return_value=document) as compose,
            patch(
                "runner.cli._worker_provider",
                side_effect=AssertionError("dry-run must not construct worker provider"),
            ),
            contextlib.redirect_stdout(output),
        ):
            status = main(
                [
                    "/project",
                    "--task",
                    "task",
                    "--task-id",
                    "t1",
                    "--provider",
                    "claude-code",
                    "--dry-run",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), document)
        compose.assert_called_once_with("/project", "task", task_id="t1")

    def test_selected_provider_preflights_before_run(self) -> None:
        order: list[str] = []
        provider = Mock()
        provider.preflight.side_effect = lambda: order.append("preflight")

        def run_after_preflight(*args: object, **kwargs: object) -> dict[str, object]:
            order.append("run")
            return {"steps": 0}

        with (
            patch("runner.cli._worker_provider", return_value=provider),
            patch("runner.cli.run", side_effect=run_after_preflight),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = main(
                ["/project", "--task", "task", "--task-id", "t1"]
            )

        self.assertEqual(status, 0)
        self.assertEqual(order, ["preflight", "run"])

    def test_codex_provider_selection(self) -> None:
        provider = Mock()
        with patch("runner.cli.CodexProvider", return_value=provider) as factory:
            selected = _worker_provider("codex", "/project")

        self.assertIs(selected, provider)
        factory.assert_called_once_with("/project")

    def test_compose_flag_is_forwarded_to_the_loop(self) -> None:
        provider = Mock()
        with (
            patch("runner.cli._worker_provider", return_value=provider),
            patch("runner.cli.run", return_value={"steps": 0}) as runner,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = main(
                [
                    "/project",
                    "--task",
                    "task",
                    "--task-id",
                    "t1",
                    "--compose",
                ]
            )

        self.assertEqual(status, 0)
        self.assertTrue(runner.call_args.kwargs["compose"])

    def test_seeded_codex_run_needs_no_anthropic_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="architect",
                task_id="seeded-codex",
                blueprint={
                    "version": 3,
                    "schema": None,
                    "contracts": {
                        "version": 2,
                        "actors": {
                            "worker": {
                                "read": ["/result"],
                                "write": ["/result"],
                            }
                        },
                    },
                    "initial_state": {"result": False},
                },
            )
            provider = Mock()
            provider.complete.return_value = Completion(
                '[{"op":"replace","path":"/result","value":true}]',
                1,
                1,
                None,
            )
            output = io.StringIO()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("runner.cli.CodexProvider", return_value=provider),
                patch(
                    "runner.loop.install",
                    side_effect=AssertionError("Architect must not run"),
                ),
                contextlib.redirect_stdout(output),
            ):
                status = main(
                    [
                        str(project),
                        "--task",
                        "finish",
                        "--task-id",
                        "seeded-codex",
                        "--provider",
                        "codex",
                        "--max-steps",
                        "1",
                    ]
                )

            self.assertEqual(status, 0)
            self.assertTrue(json.loads(output.getvalue())["final_state"]["result"])


if __name__ == "__main__":
    unittest.main()
