from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReconstructionTests(unittest.TestCase):
    def test_full_chain_reconstructs_from_hashes_and_json_alone(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        fixture = project_root / "tests" / "fixtures" / "create_three_step_tree.py"
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(project_root), environment.get("PYTHONPATH")))
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory)
            subprocess.run(
                [sys.executable, str(fixture), str(project)],
                check=True,
                cwd=project_root,
                env=environment,
            )

            state_tree = project / ".state-tree"
            state = json.loads((state_tree / "kernel.json").read_text(encoding="utf-8"))
            objects_directory = state_tree / "objects" / "sha256"
            objects: dict[str, bytes] = {}
            for object_path in objects_directory.iterdir():
                self.assertTrue(object_path.is_file())
                content = object_path.read_bytes()
                self.assertEqual(object_path.name, sha256(content).hexdigest())
                objects[object_path.name] = content

            self.assertEqual(state["format"], "state-tree")
            self.assertEqual(state["format_version"], 1)
            self.assertIsNone(state["contracts_head"])
            self.assertIsNone(state["schema_head"])
            genesis_state = f"sha256:{sha256(b'{}').hexdigest()}"
            self.assertIn(self._digest(state["state_head"]), objects)
            current_digest = self._digest(state["ledger_head"])
            reverse_chain = []
            for expected_sequence in range(state["revision"], 0, -1):
                entry = json.loads(objects[current_digest])
                self.assertEqual(
                    set(entry),
                    {
                        "actor",
                        "contracts",
                        "kind",
                        "metadata",
                        "parent_state",
                        "payload_hash",
                        "previous_hash",
                        "recorded_at",
                        "schema",
                        "sequence",
                        "state",
                        "task_id",
                        "version",
                        "view",
                    },
                )
                self.assertEqual(entry["version"], 4)
                self.assertEqual(entry["sequence"], expected_sequence)
                self.assertIn(self._digest(entry["payload_hash"]), objects)
                self.assertIn(self._digest(entry["parent_state"]), objects)
                self.assertIn(self._digest(entry["state"]), objects)
                self.assertIsNone(entry["view"])
                self.assertIsNone(entry["schema"])
                self.assertIsNone(entry["contracts"])
                reverse_chain.append(entry)
                current_digest = entry["previous_hash"]

            self.assertEqual(current_digest, "0" * 64)
            chain = list(reversed(reverse_chain))
            self.assertEqual([entry["sequence"] for entry in chain], [1, 2, 3])
            previous_state = genesis_state
            for entry in chain:
                self.assertEqual(entry["parent_state"], previous_state)
                previous_state = entry["state"]
            self.assertEqual(previous_state, state["state_head"])
            self.assertEqual(
                [entry["kind"] for entry in chain],
                ["research", "decision", "development"],
            )

    def _digest(self, reference: str) -> str:
        self.assertTrue(reference.startswith("sha256:"))
        digest = reference.removeprefix("sha256:")
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in digest))
        return digest
