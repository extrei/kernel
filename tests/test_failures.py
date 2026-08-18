from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    PatchError,
    apply_patch,
    entries,
    initialize,
    read_artifact,
    record_failure,
    set_blueprint,
    verify,
)


class FailureRecordTests(unittest.TestCase):
    def test_failure_is_an_unchanged_state_verified_ledger_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="architect",
                task_id="failure-record",
                blueprint=self._blueprint(),
            )
            before = self._kernel_state(project)

            record = record_failure(
                project,
                actor="worker",
                task_id="failure-record",
                stage="provider",
                reason="provider unavailable",
            )

            after = self._kernel_state(project)
            entry = entries(project)[-1]
            payload = json.loads(read_artifact(project, entry["payload_hash"]))
            self.assertEqual(entry["kind"], "failure")
            self.assertEqual(entry["parent_state"], entry["state"])
            self.assertEqual(entry["state"], before["state_head"])
            self.assertEqual(after["revision"], before["revision"] + 1)
            self.assertEqual(after["ledger_head"], record.entry_hash)
            self.assertEqual(after["state_head"], before["state_head"])
            self.assertEqual(after["blueprint_head"], before["blueprint_head"])
            self.assertEqual(payload, {
                "reason": "provider unavailable",
                "stage": "provider",
                "version": 1,
            })
            self.assertEqual(verify(project, strict=True), record.entry_hash)

    def test_kernel_owned_kinds_cannot_be_forged_by_apply_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]

            for kind in ("blueprint", "failure", "rejection"):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(PatchError, "reserved"):
                        apply_patch(
                            project,
                            actor="worker",
                            task_id="reserved-kind",
                            parent_state=parent,
                            patch=[],
                            kind=kind,
                        )

            set_blueprint(
                project,
                actor="architect",
                task_id="reserved-kind",
                blueprint=self._blueprint(),
            )
            self.assertEqual(entries(project)[-1]["kind"], "blueprint")

    @staticmethod
    def _blueprint() -> dict[str, object]:
        return {
            "version": 3,
            "schema": None,
            "contracts": {
                "version": 2,
                "actors": {"worker": {"read": [], "write": []}},
            },
        }

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
