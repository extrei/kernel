from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from kernel import apply_patch, set_contracts
from kernel.kernel import initialize
from kernel.mcp import create_server


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project = Path(self.temporary_directory.name)
        initialize(self.project)

    async def test_tools_bind_identity_and_exchange_text_step(self) -> None:
        artifact = self.project / "result.txt"
        artifact.write_text("runner output\n", encoding="utf-8")
        server = create_server(self.project, "claude")

        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            self.assertEqual(
                set(tools),
                {"get_view", "kernel_status", "read_artifact", "submit_step"},
            )
            self.assertTrue(tools["get_view"].annotations.read_only_hint)
            self.assertTrue(tools["kernel_status"].annotations.read_only_hint)
            self.assertTrue(tools["read_artifact"].annotations.read_only_hint)
            self.assertFalse(tools["submit_step"].annotations.read_only_hint)
            self.assertNotIn("actor", tools["submit_step"].input_schema["properties"])
            self.assertNotIn("project", tools["submit_step"].input_schema["properties"])
            self.assertIn("kind", tools["submit_step"].input_schema["required"])
            self.assertNotIn("actor", tools["get_view"].input_schema["properties"])

            status = await client.call_tool("kernel_status", {})
            self.assertFalse(status.is_error)
            self.assertEqual(status.structured_content["actor"], "claude")
            self.assertEqual(
                status.structured_content["project_root"],
                str(self.project.resolve()),
            )

            submitted = await client.call_tool(
                "submit_step",
                {
                    "artifact_path": "result.txt",
                    "kind": "result",
                    "task_id": "mcp-contract",
                },
            )
            self.assertFalse(submitted.is_error)
            record = submitted.structured_content
            self.assertEqual(record["actor"], "claude")
            self.assertEqual(record["sequence"], 1)

            read = await client.call_tool(
                "read_artifact",
                {"content_hash": record["content_hash"]},
            )
            self.assertFalse(read.is_error)
            self.assertEqual(read.structured_content["encoding"], "utf-8")
            self.assertEqual(read.structured_content["content"], "runner output\n")

    async def test_get_view_uses_only_the_launch_bound_actor(self) -> None:
        kernel_state = self._kernel_state()
        base = apply_patch(
            self.project,
            actor="setup",
            task_id="mcp-view",
            parent_state=kernel_state["state_head"],
            patch=[
                {"op": "add", "path": "/public", "value": "visible"},
                {"op": "add", "path": "/secret", "value": "hidden"},
            ],
        )
        set_contracts(
            self.project,
            actor="human",
            task_id="mcp-view",
            contracts={
                "version": 2,
                "actors": {
                    "claude": {"read": ["/public"], "write": ["/public"]},
                    "glm": {"read": ["/secret"], "write": ["/secret"]},
                },
            },
        )
        server = create_server(self.project, "claude")

        async with Client(server) as client:
            result = await client.call_tool("get_view", {})

        self.assertFalse(result.is_error)
        record = result.structured_content
        self.assertEqual(record["actor"], "claude")
        self.assertEqual(record["document"], {"public": "visible"})
        self.assertEqual(record["state"], base.state)
        self.assertTrue(record["view_hash"].startswith("sha256:"))

    async def test_binary_artifact_is_returned_as_base64(self) -> None:
        content = b"\x00\xff\x10binary"
        (self.project / "result.bin").write_bytes(content)
        server = create_server(self.project, "glm")

        async with Client(server) as client:
            submitted = await client.call_tool(
                "submit_step",
                {
                    "artifact_path": "result.bin",
                    "kind": "result",
                    "task_id": "binary-result",
                },
            )
            read = await client.call_tool(
                "read_artifact",
                {"content_hash": submitted.structured_content["content_hash"]},
            )

        self.assertEqual(read.structured_content["encoding"], "base64")
        self.assertEqual(
            base64.b64decode(read.structured_content["content"]),
            content,
        )

    async def test_tool_reports_project_boundary_failure(self) -> None:
        with tempfile.NamedTemporaryFile() as outside:
            outside.write(b"outside")
            outside.flush()
            server = create_server(self.project, "deep-seek")
            async with Client(server) as client:
                result = await client.call_tool(
                    "submit_step",
                    {
                        "artifact_path": outside.name,
                        "kind": "result",
                        "task_id": "outside-result",
                    },
                )

        self.assertTrue(result.is_error)

    def _kernel_state(self) -> dict[str, object]:
        path = self.project / ".state-tree" / "kernel.json"
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
