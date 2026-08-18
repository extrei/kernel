from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from kernel import StaleParentError, apply_patch, state
from kernel.kernel import initialize, verify


def _competing_patch(project: str, parent_state: str, winner: str) -> str:
    try:
        apply_patch(
            project,
            actor=winner,
            task_id="concurrent-state",
            parent_state=parent_state,
            patch=[{"op": "add", "path": "/winner", "value": winner}],
        )
    except StaleParentError:
        return "stale"
    return "committed"


class ConcurrentStateTests(unittest.TestCase):
    def test_two_processes_compete_from_one_parent_and_only_one_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent_state = json.loads(
                (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
            )["state_head"]

            with ProcessPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _competing_patch,
                        str(project),
                        parent_state,
                        actor,
                    )
                    for actor in ("agent-1", "agent-2")
                ]
                outcomes = [future.result(timeout=15) for future in futures]

            self.assertEqual(sorted(outcomes), ["committed", "stale"])
            kernel_state = json.loads(
                (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
            )
            self.assertEqual(kernel_state["revision"], 1)
            self.assertIn(state(project)["winner"], {"agent-1", "agent-2"})
            self.assertEqual(verify(project, strict=True), kernel_state["ledger_head"])


if __name__ == "__main__":
    unittest.main()
