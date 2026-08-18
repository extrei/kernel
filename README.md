# Project Kernel

A small, local, append-only coordination ledger for agent work. Each project owns its own `.state-tree/`; runner skills remain outside this repository.

The kernel records completed steps. It does not route agents, enforce transitions, decide outcomes, or depend on GitHub.

## Initialize a project

```text
state-tree init /path/to/project
```

This creates the local ledger boundary:

```text
.state-tree/
├── .gitignore              # excludes runtime state only
├── kernel.json             # revision plus ledger and state heads
├── kernel.lock             # ignored; recreated when a writer starts
├── cache/verified          # ignored, disposable verification checkpoint
└── objects/
    └── sha256/             # entries, artifacts, patches, and snapshots
```

Repeating initialization against a valid tree is a safe no-op; it never replaces existing state.

Every `init` call also prints a handoff contract to stdout:

```text
Handoff contract (convention, not enforcement):
  1. Register the plan before the work starts.
  2. One task per handoff. A roadmap is not a task.
  3. The worker's first write is its result file, not its last.
  4. Register the result before starting the next task.
  5. A task with one ledger entry is an open task.
```

It is printed, never written or recorded. The kernel does not check it, and a ledger that ignores it verifies exactly the same. It exists so the agent that runs `init` reads the convention before it registers a first task.

Commit `.state-tree/kernel.json`, `.state-tree/objects/`, and the generated `.state-tree/.gitignore` to carry verified history into clones. Do not commit `kernel.lock` or temporary writes. Only one canonical branch should advance the ledger because divergent heads cannot be merged.

Kernel does not invoke Git. Existing projects that ignore the entire `.state-tree/` directory must remove that parent ignore rule before adding the durable files.

For development before installing the console command:

```text
python -m kernel init /path/to/project
```

## State snapshots

`kernel.state(project)` reads the canonical JSON object referenced by `state_head`. `kernel.apply_patch(...)` applies `add`, `replace`, and `test` operations to a copy, stores both the patch and resulting snapshot as immutable objects, and advances the ledger and state heads atomically.

```python
import json
from pathlib import Path

from kernel import apply_patch, state

project = Path("/path/to/project")
kernel_state = json.loads((project / ".state-tree/kernel.json").read_text())
record = apply_patch(
    project,
    actor="codex",
    task_id="task-1",
    parent_state=kernel_state["state_head"],
    patch=[{"op": "add", "path": "/status", "value": "ready"}],
)
assert state(project) == {"status": "ready"}
```

A competing write prepared from the same parent raises `StaleParentError`. `remove` is rejected unless `allow_remove=True`; `move` and `copy` are unsupported.

Every ledger v3 entry carries `parent_state`, `state`, nullable `view`, and nullable `schema` references. Artifact-only steps set `parent_state == state`, explicitly recording that they do not change project state.

`set_schema(...)` places a valid Draft 2020-12 schema in force only when the current Snapshot satisfies it. Later patches are rejected before object storage when their candidate Snapshot violates that schema. `verify(...)` remains structural; `audit_schema(...)` explicitly re-evaluates each historical Snapshot against the schema recorded on its own ledger entry.

When a schema marks an array with `"x-kernel-collection": true`, the persisted Snapshot returned by `state()` contains `{"$collection":"sha256:…"}` at that path rather than the inlined array. Use `collection(project, pointer)` to resolve that array.

The exact one-key `$collection` object is reserved for this reference form.

Normal verification may reuse `.state-tree/cache/verified`; malformed, missing, or mismatched checkpoints fall back to genesis verification. `verify(project, strict=True)` always ignores the checkpoint. Deleting `.state-tree/cache/` is always safe.

## Record and read steps

`record_step` stores a project file as immutable bytes and appends a genesis-rooted ledger entry. A caller supplies its `task_id` and opaque `kind`; only files inside the project and outside `.state-tree/` are accepted.

A human reads the full verified ledger with:

```text
state-tree log /path/to/project
state-tree log /path/to/project --limit 10
```

Each line contains: sequence, timestamp, actor, kind, task ID, and payload hash. Agents exchange those hashes with a handoff rather than querying the ledger.

## Local MCP server

`kernel-mcp` exposes three tools over stdio:

- `kernel_status`
- `submit_step`
- `read_artifact`

The project and actor identity are fixed when the process starts and are not tool arguments. A project is required explicitly through `--project` or `KERNEL_PROJECT`:

```text
kernel-mcp --project /path/to/project --actor codex
```

The project must already contain a valid `.state-tree/`. Example project-scoped Codex configuration:

```toml
[mcp_servers.kernel]
command = "/Users/[user]/kernel/.venv/bin/kernel-mcp"
args = ["--project", "/absolute/path/to/project", "--actor", "codex"]
default_tools_approval_mode = "writes"
```

This binds attribution at the MCP boundary; it is not an operating-system security boundary. A runner that must be unable to open `.state-tree/` directly still requires filesystem sandboxing.

## Test

```text
python -m unittest discover -s tests -v
```
