# Design

## Invariant

> The project-local kernel is the sole holder of accepted state. Codex proposes; the user decides; runner skills submit hashed results; only the kernel advances state.

## Boundaries

- The kernel is local to one project.
- A blueprint is proposed before a human explicitly accepts or declines it.
- Runner skills own provider invocation; the kernel stays model-neutral.
- The local hash-object tree is the source of project provenance.
- Source may be hosted publicly, but runtime state and coordination never depend on GitHub.
- No CI workflow, watcher, architect, scheduler, or compatibility migration belongs in the runtime.

## Local layout

`state-tree init` creates `.state-tree/` inside an existing project. The directory contains a mutable kernel pointer (`kernel.json`), a local writer lock (`kernel.lock`), and immutable SHA-256 objects (`objects/sha256/<digest>`).

Initialization stores a canonical empty accepted-state object, then points the kernel at that object. The initializer does not inspect or import prior project history, create a Git repository, invoke a model, or make a decision on behalf of the user.

## Artifact ledger

Artifact bytes and ledger entries share the immutable object store. Recording an artifact stores its bytes by content hash, appends an `artifact-recorded` entry, and atomically advances `kernel.json` to that entry. It does not change the accepted-state pointer.

Every ledger entry contains its sequence and the preceding entry's hash. Sequence 1 points to the all-zero genesis hash. Validation walks the complete chain and verifies every entry object and referenced payload before returning the durable head.

## MCP boundary

The MCP server is a local stdio adapter, not a second kernel. It exposes kernel verification, artifact submission, and artifact retrieval. Each running server is bound to exactly one project path and one actor identity at launch; neither value is accepted from a tool call.

MCP tool annotations distinguish reads from the ledger-appending submission. They guide clients but are not authorization. Filesystem sandboxing remains necessary when a runner must be unable to bypass MCP and touch `.state-tree/` directly.
