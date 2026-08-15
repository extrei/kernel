from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from kernel.controller import ArtifactError, read_artifact, record_artifact
from kernel.kernel import (
    _GENESIS_HASH,
    KERNEL_STATE_FILE,
    OBJECTS_DIRECTORY,
    STATE_TREE_DIRECTORY,
    LedgerIntegrityError,
    initialize,
    verify,
)


class ArtifactRecordingTests(unittest.TestCase):
    def test_recording_stores_content_and_advances_the_ledger_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            artifact = project / "result.txt"
            artifact.write_text("agent result", encoding="utf-8")

            initial_state = self._state(project)
            record = record_artifact(
                project,
                agent="claude",
                task_id="task-1",
                artifact=artifact,
                kind="result",
            )

            state = self._state(project)
            self.assertEqual(record.sequence, 1)
            self.assertEqual(record.size, len(b"agent result"))
            self.assertEqual(state["accepted_state"], initial_state["accepted_state"])
            self.assertEqual(state["ledger_head"], record.entry_hash)
            self.assertEqual(state["revision"], 1)
            self.assertEqual(read_artifact(project, record.content_hash), b"agent result")
            self.assertEqual(verify(project), record.entry_hash)

            entry = self._object_json(project, record.entry_hash)
            self.assertEqual(entry["actor"], "claude")
            self.assertEqual(entry["kind"], "artifact-recorded")
            self.assertEqual(entry["payload_hash"], record.content_hash)
            self.assertEqual(entry["previous_hash"], _GENESIS_HASH)
            self.assertEqual(
                entry["metadata"],
                {
                    "artifact_kind": "result",
                    "bytes": len(b"agent result"),
                    "name": "result.txt",
                    "path": "result.txt",
                    "task_id": "task-1",
                },
            )

    def test_multiple_agents_extend_one_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            first_file = project / "analysis.md"
            second_file = project / "review.md"
            first_file.write_text("analysis", encoding="utf-8")
            second_file.write_text("review", encoding="utf-8")

            first = record_artifact(
                project,
                agent="claude",
                task_id="task-2",
                artifact=first_file,
                kind="analysis",
            )
            second = record_artifact(
                project,
                agent="deep-seek",
                task_id="task-2",
                artifact=second_file,
                kind="review",
            )

            second_entry = self._object_json(project, second.entry_hash)
            self.assertEqual(first.sequence, 1)
            self.assertEqual(second.sequence, 2)
            self.assertEqual(second_entry["previous_hash"], first.entry_hash.removeprefix("sha256:"))
            self.assertEqual(self._state(project)["revision"], 2)
            self.assertEqual(verify(project), second.entry_hash)

    def test_tampered_ledger_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            artifact = project / "result.txt"
            artifact.write_text("result", encoding="utf-8")
            record = record_artifact(
                project,
                agent="glm",
                task_id="task-3",
                artifact=artifact,
                kind="result",
            )

            self._object_path(project, record.entry_hash).write_text("{}", encoding="utf-8")

            with self.assertRaises(LedgerIntegrityError):
                verify(project)

    def test_tampered_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            artifact = project / "result.txt"
            artifact.write_text("result", encoding="utf-8")
            record = record_artifact(
                project,
                agent="codex",
                task_id="task-4",
                artifact=artifact,
                kind="result",
            )

            self._object_path(project, record.content_hash).write_bytes(b"changed")

            with self.assertRaises(LedgerIntegrityError):
                verify(project)

    def test_artifact_must_be_inside_project_and_outside_state_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            initialize(project)
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")

            with self.assertRaisesRegex(ArtifactError, "inside the project"):
                record_artifact(
                    project,
                    agent="codex",
                    task_id="task-5",
                    artifact=outside,
                    kind="result",
                )
            with self.assertRaisesRegex(ArtifactError, "inside .state-tree"):
                record_artifact(
                    project,
                    agent="codex",
                    task_id="task-5",
                    artifact=project / STATE_TREE_DIRECTORY / KERNEL_STATE_FILE,
                    kind="result",
                )

    @staticmethod
    def _state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / STATE_TREE_DIRECTORY / KERNEL_STATE_FILE).read_text(encoding="utf-8")
        )

    @staticmethod
    def _object_path(project: Path, reference: str) -> Path:
        algorithm, digest = reference.split(":", maxsplit=1)
        return project / STATE_TREE_DIRECTORY / OBJECTS_DIRECTORY / algorithm / digest

    @classmethod
    def _object_json(cls, project: Path, reference: str) -> dict[str, object]:
        object_path = cls._object_path(project, reference)
        content = object_path.read_bytes()
        expected_digest = reference.removeprefix("sha256:")
        if sha256(content).hexdigest() != expected_digest:
            raise AssertionError("fixture object does not match its hash")
        return json.loads(content)
