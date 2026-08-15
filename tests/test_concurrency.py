from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from kernel.controller import record_artifact
from kernel.kernel import KERNEL_STATE_FILE, STATE_TREE_DIRECTORY, initialize, verify


class ConcurrentArtifactTests(unittest.TestCase):
    def test_concurrent_agents_do_not_lose_ledger_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            initialize(project)
            artifacts = []
            for index in range(8):
                artifact = project / f"result-{index}.txt"
                artifact.write_text(f"result {index}", encoding="utf-8")
                artifacts.append(artifact)

            def record(index: int):
                return record_artifact(
                    project,
                    agent=f"agent-{index}",
                    task_id="parallel-task",
                    artifact=artifacts[index],
                    kind="result",
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                records = list(executor.map(record, range(8)))

            self.assertEqual(sorted(record.sequence for record in records), list(range(1, 9)))
            state = json.loads(
                (project / STATE_TREE_DIRECTORY / KERNEL_STATE_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(state["revision"], 8)
            self.assertEqual(verify(project), state["ledger_head"])


if __name__ == "__main__":
    unittest.main()
