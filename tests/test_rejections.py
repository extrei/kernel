from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from kernel import (
    PatchError,
    SchemaError,
    StaleParentError,
    StaleViewError,
    UnauthorizedWriteError,
    apply_patch,
    set_blueprint,
)
from kernel.kernel import (
    StateTreeError,
    _canonical_json_bytes,
    entries,
    initialize,
    read_object,
    store_object,
    verify,
)


class RejectionRecordTests(unittest.TestCase):
    def test_syntax_rejection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            candidate = {"op": "add", "path": "/value", "value": 1}

            self._reject_and_assert(
                project,
                candidate=candidate,
                error_type=PatchError,
                expected_paths=[],
                stage="syntax",
            )

    def test_stale_parent_rejection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            candidate = [{"op": "add", "path": "/value", "value": 1}]

            self._reject_and_assert(
                project,
                candidate=candidate,
                error_type=StaleParentError,
                expected_paths=[["add", "/value"]],
                parent_state=f"sha256:{'0' * 64}",
                stage="stale_parent",
            )

    def test_auth_rejection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="human",
                task_id="auth-rejection",
                blueprint=self._blueprint(
                    None,
                    {"worker": {"read": [], "write": ["/allowed"]}},
                ),
            )
            candidate = [{"op": "add", "path": "/blocked", "value": 1}]

            self._reject_and_assert(
                project,
                candidate=candidate,
                error_type=UnauthorizedWriteError,
                expected_paths=[["add", "/blocked"]],
                stage="auth",
            )

    def test_view_rejection_keeps_the_supplied_view_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="human",
                task_id="view-rejection",
                blueprint=self._blueprint(
                    None,
                    {
                        "worker": {
                            "read": ["/value"],
                            "write": ["/value"],
                        }
                    },
                ),
            )
            supplied_view = store_object(
                project, _canonical_json_bytes({"forged": True})
            )
            candidate = [{"op": "add", "path": "/value", "value": 1}]

            self._reject_and_assert(
                project,
                candidate=candidate,
                error_type=StaleViewError,
                expected_paths=[["add", "/value"]],
                stage="view",
                view=supplied_view,
            )

    def test_apply_rejection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            candidate = [{"op": "replace", "path": "/missing", "value": 1}]

            self._reject_and_assert(
                project,
                candidate=candidate,
                error_type=PatchError,
                expected_paths=[["replace", "/missing"]],
                stage="apply",
            )

    def test_schema_rejection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="human",
                task_id="schema-rejection",
                blueprint=self._blueprint(
                    {
                        "additionalProperties": False,
                        "properties": {"count": {"type": "integer"}},
                        "type": "object",
                    },
                    {"worker": {"read": ["/count"], "write": ["/count"]}},
                ),
            )
            candidate = [{"op": "add", "path": "/count", "value": "bad"}]

            self._reject_and_assert(
                project,
                candidate=candidate,
                error_type=SchemaError,
                expected_paths=[["add", "/count"]],
                stage="schema",
            )

    def test_pre_lock_identifier_failure_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            before_state = self._kernel_state(project)
            before_objects = self._object_names(project)

            with self.assertRaises(PatchError):
                apply_patch(
                    project,
                    actor="bad actor",
                    task_id="identifier",
                    parent_state=before_state["state_head"],
                    patch=[{"op": "add", "path": "/value", "value": 1}],
                )

            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(self._object_names(project), before_objects)
            self.assertEqual(entries(project), [])

    def test_rejection_write_failure_preserves_original_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]

            with mock_patch(
                "kernel.state._append_ledger_entry_locked",
                side_effect=StateTreeError("secondary failure"),
            ):
                with self.assertRaisesRegex(PatchError, "patch must be an array"):
                    apply_patch(
                        project,
                        actor="worker",
                        task_id="secondary-failure",
                        parent_state=parent,
                        patch={"not": "an array"},
                    )

            self.assertEqual(entries(project), [])

    def _reject_and_assert(
        self,
        project: Path,
        *,
        candidate: object,
        error_type: type[Exception],
        expected_paths: list[list[str]],
        stage: str,
        parent_state: str | None = None,
        view: str | None = None,
    ) -> None:
        before = self._kernel_state(project)
        with self.assertRaises(error_type):
            apply_patch(
                project,
                actor="worker",
                task_id=f"{stage}-rejection",
                parent_state=before["state_head"] if parent_state is None else parent_state,
                patch=candidate,
                view=view,
            )

        after = self._kernel_state(project)
        self.assertEqual(after["state_head"], before["state_head"])
        self.assertEqual(after["blueprint_head"], before["blueprint_head"])
        self.assertEqual(after["revision"], before["revision"] + 1)

        entry = entries(project)[-1]
        self.assertEqual(entry["kind"], "rejection")
        self.assertEqual(entry["parent_state"], before["state_head"])
        self.assertEqual(entry["state"], before["state_head"])
        self.assertEqual(entry["blueprint"], before["blueprint_head"])
        self.assertEqual(entry["view"], view)
        rejection = json.loads(read_object(project, entry["payload_hash"]))
        self.assertEqual(set(rejection), {"patch", "paths", "reason", "stage", "version"})
        self.assertEqual(rejection["version"], 1)
        self.assertEqual(rejection["stage"], stage)
        self.assertEqual(rejection["paths"], expected_paths)
        self.assertTrue(rejection["reason"])
        self.assertEqual(json.loads(read_object(project, rejection["patch"])), candidate)
        self.assertEqual(verify(project, strict=True), after["ledger_head"])

    @staticmethod
    def _blueprint(
        schema: dict[str, object] | None,
        actors: dict[str, object],
    ) -> dict[str, object]:
        return {
            "version": 2,
            "schema": schema,
            "contracts": {"version": 2, "actors": actors},
        }

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _object_names(project: Path) -> set[str]:
        directory = project / ".state-tree" / "objects" / "sha256"
        return {path.name for path in directory.iterdir()}


if __name__ == "__main__":
    unittest.main()
