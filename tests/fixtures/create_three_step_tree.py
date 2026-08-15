from pathlib import Path
import sys

from kernel.controller import record_step
from kernel.kernel import initialize


project = Path(sys.argv[1])
initialize(project)

for index, (agent, task_id, kind) in enumerate(
    (
        ("codex", "research-task", "research"),
        ("claude", "decision-task", "decision"),
        ("glm", "development-task", "development"),
    ),
    start=1,
):
    artifact = project / f"step-{index}.txt"
    artifact.write_text(f"{kind} output\n", encoding="utf-8")
    record_step(
        project,
        agent=agent,
        task_id=task_id,
        artifact=artifact,
        kind=kind,
    )
