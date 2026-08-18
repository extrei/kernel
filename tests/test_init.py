import contextlib
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest

from kernel.cli import HANDOFF_CONTRACT, main
from kernel.controller import record_step
from kernel.kernel import (
    _GENESIS_HASH,
    KERNEL_LOCK_FILE,
    KERNEL_STATE_FILE,
    OBJECTS_DIRECTORY,
    STATE_TREE_DIRECTORY,
    StateTreeError,
    entries,
    initialize,
    verify,
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
            self.assertIsNone(state["contracts_head"])
            self.assertIsNone(state["schema_head"])
            self.assertEqual(state["state_head"], f"sha256:{sha256(b'{}').hexdigest()}")
            object_directory = state_tree / OBJECTS_DIRECTORY / "sha256"
            self.assertTrue(object_directory.is_dir())
            self.assertEqual(
                [path.name for path in object_directory.iterdir()], [sha256(b"{}").hexdigest()]
            )
            self.assertEqual(
                (state_tree / ".gitignore").read_text(encoding="utf-8"),
                "/kernel.lock\n"
                "/.kernel.json.tmp-*\n"
                "/objects/sha256/.*.tmp-*\n"
                "/cache/\n",
            )

    def test_clone_without_runtime_lock_can_verify_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            lock_file = project / STATE_TREE_DIRECTORY / KERNEL_LOCK_FILE
            lock_file.unlink()

            self.assertEqual(verify(project), f"sha256:{_GENESIS_HASH}")
            artifact = project / "result.txt"
            artifact.write_text("result", encoding="utf-8")
            record_step(project, agent="a", task_id="clone", artifact=artifact, kind="test")

            self.assertTrue(lock_file.is_file())

            lock_file.unlink()
            lock_file.symlink_to(project / "not-a-lock")
            with self.assertRaisesRegex(StateTreeError, "Cannot create or open kernel lock"):
                record_step(
                    project,
                    agent="agent",
                    task_id="clone-test",
                    artifact=artifact,
                    kind="test",
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

    def test_cli_init_states_the_handoff_contract_on_every_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()

            first = io.StringIO()
            with contextlib.redirect_stdout(first):
                self.assertEqual(main(["init", str(project)]), 0)
            repeat = io.StringIO()
            with contextlib.redirect_stdout(repeat):
                self.assertEqual(main(["init", str(project)]), 0)

            for output in (first.getvalue(), repeat.getvalue()):
                self.assertIn(HANDOFF_CONTRACT, output)
                self.assertIn("convention, not enforcement", output)

            state_tree = project / STATE_TREE_DIRECTORY
            self.assertEqual(
                sorted(path.name for path in state_tree.iterdir()),
                sorted(
                    [".gitignore", KERNEL_LOCK_FILE, KERNEL_STATE_FILE, OBJECTS_DIRECTORY]
                ),
            )

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
