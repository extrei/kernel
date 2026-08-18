from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from runner.cli import main


class RunnerCLITests(unittest.TestCase):
    def test_dry_run_uses_architect_api_path_and_installs_nothing(self) -> None:
        document = {
            "version": 2,
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


if __name__ == "__main__":
    unittest.main()
