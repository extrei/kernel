import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from kernel.cli import main
from kernel.controller import record_step
from kernel.kernel import (
    _GENESIS_HASH,
    KERNEL_STATE_FILE,
    OBJECTS_DIRECTORY,
    STATE_TREE_DIRECTORY,
    StateTreeError,
    entries,
    initialize,
)


class StateTreeInitializationTests(unittest.TestCase):
    def test_initialization_creates_an_empty_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()

            result = initialize(project)

            self.assertTrue(result.created)
            state_tree = project / STATE_TREE_DIRECTORY
            state = json.loads((state_tree / KERNEL_STATE_FILE).read_text())

            self.assertEqual(state["format"], "state-tree")
            self.assertEqual(state["format_version"], 1)
            self.assertEqual(state["ledger_head"], f"sha256:{_GENESIS_HASH}")
            self.assertEqual(state["revision"], 0)
            object_directory = state_tree / OBJECTS_DIRECTORY / "sha256"
            self.assertTrue(object_directory.is_dir())
            self.assertEqual(list(object_directory.iterdir()), [])

    def test_valid_existing_tree_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)

            state_file = project / STATE_TREE_DIRECTORY / KERNEL_STATE_FILE
            before = state_file.read_bytes()
            result = initialize(project)

            self.assertFalse(result.created)
            self.assertEqual(state_file.read_bytes(), before)

    def test_incomplete_existing_tree_is_rejected_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            state_tree = project / STATE_TREE_DIRECTORY
            state_tree.mkdir(parents=True)
            marker = state_tree / "keep-me"
            marker.write_text("do not replace", encoding="utf-8")

            with self.assertRaises(StateTreeError):
                initialize(project)

            self.assertEqual(marker.read_text(encoding="utf-8"), "do not replace")

    def test_cli_reports_initialization_and_safe_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["init", str(project)]), 0)
                self.assertEqual(main(["init", str(project)]), 0)

            self.assertIn("Initialized state tree", output.getvalue())
            self.assertIn("Already initialized state tree", output.getvalue())

    def test_cli_log_prints_three_verified_entries_and_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            for index, kind in enumerate(("research", "decision", "development"), start=1):
                artifact = project / f"step-{index}.txt"
                artifact.write_text(f"step {index}", encoding="utf-8")
                record_step(
                    project,
                    agent=f"agent-{index}",
                    task_id="log-test",
                    artifact=artifact,
                    kind=kind,
                )

            ledger_entries = entries(project)
            fields = (
                "sequence",
                "recorded_at",
                "actor",
                "kind",
                "task_id",
                "payload_hash",
            )
            expected_lines = [
                " ".join(str(entry[field]) for field in fields)
                for entry in ledger_entries
            ]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["log", str(project)]), 0)
            self.assertEqual(output.getvalue().splitlines(), expected_lines)

            limited_output = io.StringIO()
            with contextlib.redirect_stdout(limited_output):
                self.assertEqual(main(["log", str(project), "--limit", "2"]), 0)
            self.assertEqual(limited_output.getvalue().splitlines(), expected_lines[-2:])

    def test_cli_rejects_a_missing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_project = Path(temporary_directory) / "missing"
            error = io.StringIO()

            with contextlib.redirect_stderr(error):
                self.assertEqual(main(["init", str(missing_project)]), 2)

            self.assertIn("Project directory does not exist", error.getvalue())


if __name__ == "__main__":
    unittest.main()
