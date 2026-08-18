import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import kernel.kernel as kernel_module
from kernel.controller import record_step
from kernel.kernel import LedgerIntegrityError, initialize, verify


class CheckpointTests(unittest.TestCase):
    def test_missing_and_corrupt_checkpoint_fall_back_and_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            records = self._record_steps(project, 2)
            checkpoint = project / ".state-tree" / "cache" / "verified"

            verify(project)
            self.assertEqual(self._checkpoint(checkpoint)["sequence"], 2)
            shutil.rmtree(checkpoint.parent)
            self.assertEqual(verify(project), records[-1].entry_hash)
            self.assertEqual(self._checkpoint(checkpoint)["sequence"], 2)

            checkpoint.write_text("{broken", encoding="utf-8")
            self.assertEqual(verify(project), records[-1].entry_hash)
            rebuilt = self._checkpoint(checkpoint)
            self.assertEqual(rebuilt["format_version"], 2)
            self.assertEqual(rebuilt["entry_hash"], records[-1].entry_hash.removeprefix("sha256:"))

    def test_wrong_checkpoint_hash_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            records = self._record_steps(project, 2)
            checkpoint = project / ".state-tree" / "cache" / "verified"
            checkpoint.parent.mkdir(exist_ok=True)
            checkpoint.write_text(
                json.dumps(
                    {
                        "entry_hash": records[1].entry_hash.removeprefix("sha256:"),
                        "format_version": 2,
                        "sequence": 1,
                    }
                ),
                encoding="utf-8",
            )
            first_entry = self._entry(project, records[0].entry_hash)
            payload = (
                project
                / ".state-tree"
                / "objects"
                / "sha256"
                / first_entry["payload_hash"].removeprefix("sha256:")
            )
            payload.write_bytes(b"tampered")

            with self.assertRaises(LedgerIntegrityError):
                verify(project)

    def test_inapplicable_checkpoint_metadata_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            records = self._record_steps(project, 2)
            checkpoint = project / ".state-tree" / "cache" / "verified"
            head = records[-1].entry_hash.removeprefix("sha256:")

            invalid_checkpoints = (
                {"entry_hash": head, "format_version": 1, "sequence": 2},
                {"entry_hash": head, "format_version": 2, "sequence": 3},
            )
            for invalid in invalid_checkpoints:
                with self.subTest(checkpoint=invalid):
                    checkpoint.write_text(json.dumps(invalid), encoding="utf-8")
                    self.assertEqual(verify(project), records[-1].entry_hash)
                    self.assertEqual(self._checkpoint(checkpoint)["sequence"], 2)

    def test_checkpoint_write_failure_does_not_fail_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            records = self._record_steps(project, 2)
            checkpoint = project / ".state-tree" / "cache" / "verified"
            shutil.rmtree(checkpoint.parent)

            with patch(
                "kernel.kernel.os.replace",
                side_effect=PermissionError("read-only checkout"),
            ):
                self.assertEqual(verify(project), records[-1].entry_hash)
            self.assertFalse(checkpoint.exists())

    def test_strict_verification_never_reads_a_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            records = self._record_steps(project, 2)
            verify(project)

            with patch(
                "kernel.kernel._read_checkpoint",
                side_effect=AssertionError("checkpoint must not be read"),
            ):
                self.assertEqual(verify(project, strict=True), records[-1].entry_hash)

    def test_checkpoint_at_previous_revision_verifies_the_new_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            records = self._record_steps(project, 2)
            verify(project)
            artifact = project / "step-3.txt"
            artifact.write_text("step 3", encoding="utf-8")
            third = record_step(
                project,
                agent="agent-3",
                task_id="checkpoint",
                artifact=artifact,
                kind="result",
            )

            with patch(
                "kernel.kernel._read_hashed_object",
                wraps=kernel_module._read_hashed_object,
            ) as read_hashed_object:
                self.assertEqual(verify(project), third.entry_hash)
            labels = [call.kwargs["label"] for call in read_hashed_object.call_args_list]
            self.assertFalse(any("sequence 1" in label for label in labels))
            self.assertTrue(any("sequence 2" in label for label in labels))
            checkpoint = self._checkpoint(
                project / ".state-tree" / "cache" / "verified"
            )
            self.assertEqual(checkpoint["sequence"], 3)
            self.assertNotEqual(records[-1].entry_hash, third.entry_hash)

    @staticmethod
    def _record_steps(project: Path, count: int):
        initialize(project)
        records = []
        for index in range(1, count + 1):
            artifact = project / f"step-{index}.txt"
            artifact.write_text(f"step {index}", encoding="utf-8")
            records.append(
                record_step(
                    project,
                    agent=f"agent-{index}",
                    task_id="checkpoint",
                    artifact=artifact,
                    kind="result",
                )
            )
        return records

    @staticmethod
    def _checkpoint(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _entry(project: Path, reference: str) -> dict[str, object]:
        path = (
            project
            / ".state-tree"
            / "objects"
            / "sha256"
            / reference.removeprefix("sha256:")
        )
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
