from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcp import Client

from kernel.kernel import initialize
from kernel.mcp import create_server


class RealUseCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_agents_exchange_steps_and_read_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)

            handoff = project / "handoff.md"
            handoff.write_text(
                "# Handoff\n\nCapture the research result for the next agent.\n",
                encoding="utf-8",
            )

            coordinator_server = create_server(project, "codex")
            async with Client(coordinator_server) as coordinator:
                submitted_handoff = await coordinator.call_tool(
                    "submit_step",
                    {
                        "task_id": "agent-handoff",
                        "artifact_path": "handoff.md",
                        "kind": "research",
                    },
                )
            handoff_hash = submitted_handoff.structured_content["content_hash"]

            runner_server = create_server(project, "claude")
            async with Client(runner_server) as runner:
                received_handoff = await runner.call_tool(
                    "read_artifact",
                    {"content_hash": handoff_hash},
                )
                self.assertIn(
                    "research result",
                    received_handoff.structured_content["content"],
                )

                result = project / "result.txt"
                result.write_text(
                    f"Completed handoff {handoff_hash}\n",
                    encoding="utf-8",
                )
                submitted_result = await runner.call_tool(
                    "submit_step",
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
                f"Completed handoff {handoff_hash}\n",
            )
            self.assertEqual(status.structured_content["ledger_head"], result_record["entry_hash"])


if __name__ == "__main__":
    unittest.main()
