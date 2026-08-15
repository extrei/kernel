import contextlib
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest

from kernel.cli import main
from kernel.kernel import (
    _GENESIS_HASH,
    HASH_ALGORITHM,
    KERNEL_STATE_FILE,
    OBJECTS_DIRECTORY,
    STATE_TREE_DIRECTORY,
    StateTreeError,
    initialize,
)


class StateTreeInitializationTests(unittest.TestCase):
    def test_initialization_creates_a_hashed_empty_accepted_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()

            result = initialize(project)

            self.assertTrue(result.created)
            state_tree = project / STATE_TREE_DIRECTORY
            state = json.loads((state_tree / KERNEL_STATE_FILE).read_text())
            algorithm, digest = state["accepted_state"].split(":", maxsplit=1)
            object_file = state_tree / OBJECTS_DIRECTORY / algorithm / digest

            self.assertEqual(algorithm, HASH_ALGORITHM)
            self.assertEqual(state["format"], "state-tree")
            self.assertEqual(state["format_version"], 1)
            self.assertEqual(state["ledger_head"], f"sha256:{_GENESIS_HASH}")
            self.assertEqual(state["revision"], 0)
            self.assertTrue(object_file.is_file())
            self.assertEqual(sha256(object_file.read_bytes()).hexdigest(), digest)
            self.assertEqual(
                json.loads(object_file.read_text()),
                {"kind": "accepted-state", "value": {}, "version": 1},
            )

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

    def test_cli_rejects_a_missing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_project = Path(temporary_directory) / "missing"
            error = io.StringIO()

            with contextlib.redirect_stderr(error):
                self.assertEqual(main(["init", str(missing_project)]), 2)

            self.assertIn("Project directory does not exist", error.getvalue())


if __name__ == "__main__":
    unittest.main()
