# Project Kernel

A small, local, append-only coordination ledger for agent work. Each project owns its own `.state-tree/`; runner skills remain outside this repository.

The kernel records work, accepts or rejects state transitions, and derives advisory Circuit Verdicts and Schedules. It never dispatches agents, executes a Schedule, or depends on GitHub.

## Initialize a project

```text
state-tree init /path/to/project
```

This creates the local ledger boundary:

```text
.state-tree/
├── .gitignore              # excludes runtime state only
├── kernel.json             # revision plus ledger, state, and Blueprint heads
├── kernel.lock             # ignored; recreated when a writer starts
├── cache/verified          # ignored, disposable verification checkpoint
└── objects/
    └── sha256/             # entries, artifacts, patches, snapshots, authorities
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

A competing write prepared from the same parent raises `StaleParentError`. `remove` is rejected unless the active write contract grants the path and sets `allow_remove` for the actor; there is no caller-supplied override. `move` and `copy` are unsupported.

Every ledger v5 entry carries `parent_state`, `state`, nullable `view`, and nullable `blueprint` references. Artifact-only steps set `parent_state == state`, explicitly recording that they do not change project state.

## Blueprint authority

Schema and Write Contracts enter the kernel only as one versioned Blueprint:

```json
{
  "version": 2,
  "schema": {"type": "object"},
  "contracts": {"version": 2, "actors": {}},
  "rules": [
    {"on": {"op": "add", "path": "/claims/*"}, "wake": "verifier"}
  ],
  "circuit": {"consecutive_rejections": 2, "cycle_window": 3}
}
```

`rules` and `circuit` are optional. The circuit defaults shown above apply when its values are absent. `set_blueprint(...)` validates this document against the hand-written `meta_schema()`, checks the Draft 2020-12 Schema and version 2 Write Contracts together, validates every Workflow Rule and Circuit Policy, validates the current live Snapshot, and advances one `blueprint_head` in the same ledger commit. Blueprint version 1 is rejected. There are no independent Schema or Contract installation functions.

`blueprint(...)` returns the authority document in force. `schema(...)` and `contracts(...)` are projections of it. Later patches are rejected before object storage when their candidate Snapshot violates the active Schema. `verify(...)` remains structural; `audit_schema(...)` explicitly re-evaluates each historical Snapshot through the Blueprint recorded on its own ledger entry.

When a schema marks an array with `"x-kernel-collection": true`, the persisted Snapshot returned by `state()` contains `{"$collection":"sha256:…"}` at that path rather than the inlined array. Use `collection(project, pointer)` to resolve that array.

A Blueprint is rejected when a declared Collection is unreachable through every actor's `read` and `write` patterns.

The exact one-key `$collection` object is reserved for this reference form.

A version 2 Write Contract grants actor-specific authority. `add`, `replace`, and `remove` require a matching `write` pattern; `test` requires a matching `read` pattern. Patterns match complete JSON Pointer segments, so `/plan` does not grant `/plan/status`, while `/plan/*` grants exactly one child segment. An active Blueprint rejects actors its Write Contract does not name.

With no Blueprint in force, non-removal patches remain allowed and `remove` fails closed. Authorization runs before collection hydration, patch evaluation, schema validation, or Snapshot storage. The candidate Patch is retained first so a refusal remains auditable. `verify(...)` checks Blueprint objects structurally; `audit_contracts(...)` is the explicit human audit of historical patch authority.

An actor rule may set a positive `budget`, measured in characters of canonical JSON. `view(project, actor=...)` derives and stores that actor's deterministic subdocument from the current Snapshot and the Write Contract and Schema bound by one Blueprint. Visible schema fragments appear under `$schema`; Collection references remain opaque handles.

When a View exceeds its budget, large visible entries are replaced deterministically with `{"$elided":{"bytes":N,"hash":"sha256:…"}}`. The hash resolves to the canonical original value through the existing object read path. If even the elided form cannot fit, View derivation fails.

The exact one-key `$elided` object is reserved for this View reference form.

A budgeted actor must pass the derived View hash to `apply_patch(...)`. The kernel re-derives it from `parent_state` before hydration or patch evaluation; a mismatch raises `StaleViewError`. Unbudgeted contracts may omit the View, but any supplied contracted View is still checked. `verify(...)` remains structural, while `audit_views(...)` explicitly re-evaluates historical View bindings.

Normal verification may reuse `.state-tree/cache/verified`; malformed, missing, or mismatched checkpoints fall back to genesis verification. `verify(project, strict=True)` always ignores the checkpoint. Deleting `.state-tree/cache/` is always safe.

## Rejections, circuits, and schedules

An attributable Patch refusal appends a ledger v5 `rejection` entry and re-raises the original exception. Its canonical payload records the candidate Patch hash, touched paths, reason, and one of six stages: `syntax`, `stale_parent`, `auth`, `view`, `apply`, or `schema`. A rejection advances the ledger revision but keeps `parent_state == state`, so neither the State Head nor Blueprint Head changes.

`circuit(project)` derives a deterministic `CircuitVerdict` from accepted and rejected ledger facts. It can advise `continue`, `retry`, `switch_actor`, `tighten_budget`, or `halt`; it never performs that action. `schedule(project)` matches the newest accepted Patch against the active Blueprint's Workflow Rules and returns the implied actor events. It does not invoke, dispatch, retry, queue, or write anything.

The same verdict is available to a human operator:

```text
state-tree circuit /path/to/project
```

`state-tree log` marks rejection entries with `[REJECTED]`. No circuit or scheduling surface is exposed through the worker MCP server.

## Record and read steps

`record_step` stores a project file as immutable bytes and appends a genesis-rooted ledger entry. A caller supplies its `task_id` and opaque `kind`; only files inside the project and outside `.state-tree/` are accepted.

A human reads the full verified ledger with:

```text
state-tree log /path/to/project
state-tree log /path/to/project --limit 10
```

Each line contains: sequence, timestamp, actor, kind, task ID, and payload hash. Agents exchange those hashes with a handoff rather than querying the ledger.

## Local MCP server

`kernel-mcp` exposes five tools over stdio:

- `get_view`
- `submit_patch`
- `kernel_status`
- `submit_step`
- `read_artifact`

`get_view` and `submit_patch` are the agent loop: read the bound actor's View, then propose a patch quoting the `state` and `view_hash` that View reported. The patch is refused unless it satisfies patch syntax, the actor's write contract, the view it was prepared from, patch application, and the blueprint schema. A refusal raises and is recorded in the ledger as a `rejection` entry attributed to that actor.

`submit_patch` does not accept a `kind`; every entry it writes is a `patch`. Reserved kinds stay unreachable from an agent, so a worker cannot forge the `rejection` entries `state-tree circuit` counts.

`submit_step` remains the path for raw project files — a verbatim result log is evidence, and forcing it into JSON would destroy what makes it evidence.

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

## Model runner

`kernel-run` is a separate top-level package over the public kernel interface. The kernel still never invokes a model. The runner asks an API-only Architect for a Blueprint, lets the kernel validate and install it, then submits View-bound worker Patches until the Circuit Verdict halts or the step budget is exhausted.

```text
kernel-run /path/to/project --task "Implement the accepted task" --task-id task-1
kernel-run /path/to/project --task "Show the proposed authority" --task-id task-1 --dry-run
```

Worker providers:

- `--provider api` is the default. It uses a bare `anthropic.Anthropic()` credential chain, `claude-opus-5`, adaptive thinking, high effort, and API token billing. The runner never prompts for a key.
- `--provider claude-code` runs `claude -p` with JSON output and Claude Code's reported usage and cost. The Architect still uses the API provider.

For Claude Code subscription billing, leave `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` unset and authenticate the CLI interactively. Exporting either variable routes Claude Code through API credentials instead. The runner does not branch on or rewrite the credential environment.

Claude Code receives only the configured kernel MCP tool allowlist and is never launched with `--dangerously-skip-permissions`. This is a guardrail, not a security boundary: OS-level filesystem sandboxing is still required to prevent a worker process from reading or deleting `.state-tree/` through another path.

The result reports worker input and output tokens, provider cost when known, accepted and rejected attempts, and final State. API measurements describe this architecture directly. Claude Code measurements also include its own system prompt, tools, and project instructions.

## Test

```text
python -m unittest discover -s tests -v
```
