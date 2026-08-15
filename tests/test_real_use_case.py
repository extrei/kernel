from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from kernel.kernel import KERNEL_STATE_FILE, STATE_TREE_DIRECTORY, initialize
from kernel.mcp import create_server


class RealUseCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_hands_blueprint_to_runner_and_reads_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            state_file = project / STATE_TREE_DIRECTORY / KERNEL_STATE_FILE
            accepted_before = json.loads(state_file.read_text(encoding="utf-8"))[
                "accepted_state"
            ]

            blueprint = project / "blueprint.md"
            blueprint.write_text(
                "# Blueprint\n\nImplement the approved artifact handoff.\n",
                encoding="utf-8",
            )

            coordinator_server = create_server(project, "codex")
            async with Client(coordinator_server) as coordinator:
                submitted_blueprint = await coordinator.call_tool(
                    "submit_artifact",
                    {
                        "task_id": "agent-handoff",
                        "artifact_path": "blueprint.md",
                        "kind": "blueprint",
                    },
                )
            blueprint_hash = submitted_blueprint.structured_content["content_hash"]

            runner_server = create_server(project, "claude")
            async with Client(runner_server) as runner:
                received_blueprint = await runner.call_tool(
                    "read_artifact",
                    {"content_hash": blueprint_hash},
                )
                self.assertIn(
                    "approved artifact handoff",
                    received_blueprint.structured_content["content"],
                )

                result = project / "result.txt"
                result.write_text(
                    f"Completed blueprint {blueprint_hash}\n",
                    encoding="utf-8",
                )
                submitted_result = await runner.call_tool(
                    "submit_artifact",
                    {
                        "task_id": "agent-handoff",
                        "artifact_path": "result.txt",
                        "kind": "result",
                    },
                )
            result_record = submitted_result.structured_content

            async with Client(coordinator_server) as coordinator:
                received_result = await coordinator.call_tool(
                    "read_artifact",
                    {"content_hash": result_record["content_hash"]},
                )
                status = await coordinator.call_tool("kernel_status", {})

            self.assertEqual(result_record["actor"], "claude")
            self.assertEqual(result_record["sequence"], 2)
            self.assertEqual(
                received_result.structured_content["content"],
                f"Completed blueprint {blueprint_hash}\n",
            )
            self.assertEqual(status.structured_content["ledger_head"], result_record["entry_hash"])
            accepted_after = json.loads(state_file.read_text(encoding="utf-8"))[
                "accepted_state"
            ]
            self.assertEqual(accepted_after, accepted_before)


if __name__ == "__main__":
    unittest.main()
