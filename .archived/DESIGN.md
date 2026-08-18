# Design

## Invariant

> The project-local kernel records immutable, hash-chained steps. It does not interpret or sequence them.

## Boundaries

- The kernel is local to one project.
- A human reads the ledger at the end; the kernel has no approval gate today.
- It is domain-neutral: research, marketing, decision, and development tasks record the same way.
- Runner skills own provider invocation and transition rules; the kernel is not an orchestrator.
- The local hash-object tree is the source of project provenance.
- Unknown `format_version` values fail closed. Migrations are never written.
- No watcher, scheduler, background process, CI workflow, or compatibility layer belongs in the runtime.
- Durable state-tree files are portable; writer locks and atomic-write temporaries are runtime state.
- One canonical Git branch advances a ledger. Merging divergent ledger heads is unsupported.

## Local layout

`state-tree init` creates `.state-tree/` inside an existing project:

```text
.state-tree/
├── .gitignore              # excludes runtime state only
├── kernel.json             # format, ledger head, and revision
├── kernel.lock             # ignored; created when a writer starts
└── objects/
    └── sha256/             # immutable content-addressed bytes
```

The generated ignore rules leave `kernel.json` and `objects/` visible to an enclosing Git repository while excluding the lock and temporary writes. A valid empty content object preserves the object directory before the first step. A clone therefore verifies without a lock and recreates it on the next write.

Initialization does not invoke Git, inspect or import prior history, create a repository, invoke a model, or make a decision.

`state-tree init` prints a five-line handoff contract on every call, including the no-op repeat. The contract is stdout only: it is never written to `.state-tree/`, never recorded as a step, and never checked. A ledger that violates every line still verifies. It states ledger discipline that holds for any runner in any domain, and says nothing about provider invocation, transport, or transition rules, which remain the runner skill's concern.

## Step ledger

`record_step` stores one project file by content hash and atomically appends a ledger entry. The caller supplies opaque, charset-validated `kind` and `task_id` values. The entry records its actor, sequence, predecessor hash, timestamp, payload hash, and path metadata.

Sequence 1 points to the all-zero genesis hash. Verification walks the complete chain and verifies every ledger object and referenced payload before returning any result.

The format is deliberately inspectable: an operator can verify object names with `sha256sum` and follow the JSON references with `jq`, without importing kernel code.

## Reading

`state-tree log [project] [--limit N]` is the human ledger read surface. It walks the verified chain and prints oldest-first entries as sequence, timestamp, actor, kind, task ID, and payload hash.

Agents have no ledger query API. A handoff supplies a known content hash, which another agent may retrieve with `read_artifact`.

## MCP boundary

The MCP server is a local stdio adapter, not a second kernel. It exposes `kernel_status`, `submit_step`, and `read_artifact`. Each process is bound to exactly one project path and actor identity at launch; neither comes from a tool call, and a missing project binding fails closed.

Tool annotations guide clients but are not authorization. Filesystem sandboxing is still necessary when a runner must be unable to bypass MCP and touch `.state-tree/` directly.
