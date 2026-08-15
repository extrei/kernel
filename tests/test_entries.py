from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from kernel.controller import record_step
from kernel.kernel import LedgerIntegrityError, entries, initialize


class EntriesTests(unittest.TestCase):
    def test_entries_are_oldest_first_and_fully_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            initialize(project)
            records = []
            for index, kind in enumerate(("research", "decision", "development"), start=1):
                artifact = project / f"step-{index}.txt"
                artifact.write_text(f"step {index}", encoding="utf-8")
                records.append(
                    record_step(
                        project,
                        agent=f"agent-{index}",
                        task_id="shared-task",
                        artifact=artifact,
                        kind=kind,
                    )
                )

            ledger_entries = entries(project)

            self.assertEqual([entry["sequence"] for entry in ledger_entries], [1, 2, 3])
            self.assertEqual(
                [entry["kind"] for entry in ledger_entries],
                ["research", "decision", "development"],
            )
            self.assertEqual(
                [entry["payload_hash"] for entry in ledger_entries],
                [record.content_hash for record in records],
            )
            self.assertTrue(all(entry["task_id"] == "shared-task" for entry in ledger_entries))

    def test_entries_reject_missing_or_malformed_task_id(self) -> None:
        for replacement in (None, 7):
            with self.subTest(replacement=replacement):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    project = Path(temporary_directory)
                    initialize(project)
                    artifact = project / "step.txt"
                    artifact.write_text("step", encoding="utf-8")
                    record = record_step(
                        project,
                        agent="codex",
                        task_id="schema-check",
                        artifact=artifact,
                        kind="research",
                    )
                    entry_path = (
                        project
                        / ".state-tree"
                        / "objects"
                        / "sha256"
                        / record.entry_hash.removeprefix("sha256:")
                    )
                    entry = json.loads(entry_path.read_text(encoding="utf-8"))
                    if replacement is None:
                        del entry["task_id"]
                    else:
                        entry["task_id"] = replacement
                    content = json.dumps(
                        entry,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    digest = sha256(content).hexdigest()
                    (entry_path.parent / digest).write_bytes(content)
                    state_path = project / ".state-tree" / "kernel.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["ledger_head"] = f"sha256:{digest}"
                    state_path.write_text(
                        json.dumps(state, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                    with self.assertRaises(LedgerIntegrityError):
                        entries(project)

    def test_entries_rejects_a_tampered_chain_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            initialize(project)
            artifact = project / "step.txt"
            artifact.write_text("step", encoding="utf-8")
            record = record_step(
                project,
                agent="codex",
                task_id="tamper-check",
                artifact=artifact,
                kind="research",
            )
            entry_path = (
                project
                / ".state-tree"
                / "objects"
                / "sha256"
                / record.entry_hash.removeprefix("sha256:")
            )
            entry_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(LedgerIntegrityError):
                entries(project)
