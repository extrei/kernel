from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from kernel import apply_patch, entries, set_blueprint, state
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
                {
                    "get_view",
                    "kernel_status",
                    "read_artifact",
                    "submit_patch",
                    "submit_step",
                },
            )
            self.assertTrue(tools["get_view"].annotations.read_only_hint)
            self.assertTrue(tools["kernel_status"].annotations.read_only_hint)
            self.assertTrue(tools["read_artifact"].annotations.read_only_hint)
            self.assertFalse(tools["submit_step"].annotations.read_only_hint)
            self.assertFalse(tools["submit_patch"].annotations.read_only_hint)
            self.assertNotIn("actor", tools["submit_step"].input_schema["properties"])
            self.assertNotIn("project", tools["submit_step"].input_schema["properties"])
            self.assertIn("kind", tools["submit_step"].input_schema["required"])
            self.assertNotIn("actor", tools["get_view"].input_schema["properties"])

            patch_properties = tools["submit_patch"].input_schema["properties"]
            self.assertNotIn("actor", patch_properties)
            self.assertNotIn("project", patch_properties)
            # kind stays unexposed so an agent cannot forge a "rejection" entry,
            # which kernel.circuit counts when deciding to halt.
            self.assertNotIn("kind", patch_properties)

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
        set_blueprint(
            self.project,
            actor="human",
            task_id="mcp-view",
            blueprint={
                "version": 2,
                "schema": None,
                "contracts": {
                "version": 2,
                "actors": {
                    "claude": {"read": ["/public"], "write": ["/public"]},
                    "glm": {"read": ["/secret"], "write": ["/secret"]},
                },
                },
            },
        )
        server = create_server(self.project, "claude")

        async with Client(server) as client:
            result = await client.call_tool("get_view", {})

        self.assertFalse(result.is_error)
        record = result.structured_content
        self.assertEqual(record["actor"], "claude")
        self.assertIn("blueprint", record)
        self.assertNotIn("contracts", record)
        self.assertNotIn("schema", record)
        self.assertEqual(record["document"], {"public": "visible"})
        self.assertEqual(record["state"], base.state)
        self.assertTrue(record["view_hash"].startswith("sha256:"))

    async def test_view_then_patch_closes_the_agent_loop(self) -> None:
        self._install_two_actor_blueprint()
        server = create_server(self.project, "claude")

        async with Client(server) as client:
            seen = (await client.call_tool("get_view", {})).structured_content
            submitted = await client.call_tool(
                "submit_patch",
                {
                    "task_id": "mcp-patch",
                    "parent_state": seen["state"],
                    "view": seen["view_hash"],
                    "patch": [
                        {"op": "replace", "path": "/public", "value": "written"}
                    ],
                },
            )

        self.assertFalse(submitted.is_error)
        record = submitted.structured_content
        self.assertEqual(record["actor"], "claude")
        self.assertEqual(record["kind"], "patch")
        self.assertEqual(record["parent_state"], seen["state"])
        self.assertEqual(record["view"], seen["view_hash"])
        self.assertEqual(state(self.project)["public"], "written")

    async def test_patch_outside_the_actor_write_contract_is_refused(self) -> None:
        self._install_two_actor_blueprint()
        before = self._kernel_state()
        server = create_server(self.project, "claude")

        async with Client(server) as client:
            seen = (await client.call_tool("get_view", {})).structured_content
            result = await client.call_tool(
                "submit_patch",
                {
                    "task_id": "mcp-patch",
                    "parent_state": seen["state"],
                    "view": seen["view_hash"],
                    "patch": [
                        {"op": "replace", "path": "/secret", "value": "stolen"}
                    ],
                },
            )

        self.assertTrue(result.is_error)
        after = self._kernel_state()
        self.assertEqual(after["state_head"], before["state_head"])
        self.assertEqual(state(self.project)["secret"], "hidden")
        # The refusal is a fact, attributed to the bound actor.
        refusal = entries(self.project)[-1]
        self.assertEqual(refusal["kind"], "rejection")
        self.assertEqual(refusal["actor"], "claude")

    async def test_patch_from_a_superseded_state_is_refused(self) -> None:
        self._install_two_actor_blueprint()
        server = create_server(self.project, "claude")

        async with Client(server) as client:
            seen = (await client.call_tool("get_view", {})).structured_content
            apply_patch(
                self.project,
                actor="setup",
                task_id="mcp-race",
                parent_state=seen["state"],
                patch=[{"op": "replace", "path": "/public", "value": "moved"}],
            )
            result = await client.call_tool(
                "submit_patch",
                {
                    "task_id": "mcp-patch",
                    "parent_state": seen["state"],
                    "view": seen["view_hash"],
                    "patch": [
                        {"op": "replace", "path": "/public", "value": "late"}
                    ],
                },
            )

        self.assertTrue(result.is_error)
        self.assertEqual(state(self.project)["public"], "moved")

    def _install_two_actor_blueprint(self) -> None:
        apply_patch(
            self.project,
            actor="setup",
            task_id="mcp-setup",
            parent_state=self._kernel_state()["state_head"],
            patch=[
                {"op": "add", "path": "/public", "value": "visible"},
                {"op": "add", "path": "/secret", "value": "hidden"},
            ],
        )
        set_blueprint(
            self.project,
            actor="human",
            task_id="mcp-setup",
            blueprint={
                "version": 2,
                "schema": None,
                "contracts": {
                    "version": 2,
                    "actors": {
                        "claude": {"read": ["/public"], "write": ["/public"]},
                        "setup": {
                            "read": ["/public", "/secret"],
                            "write": ["/public", "/secret"],
                        },
                    },
                },
            },
        )

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
