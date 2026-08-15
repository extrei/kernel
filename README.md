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
├── kernel.json             # format, revision, and ledger-head reference
├── kernel.lock             # serializes concurrent writes
└── objects/
    └── sha256/             # immutable, content-addressed bytes
```

Repeating initialization against a valid tree is a safe no-op; it never replaces existing state.

For development before installing the console command:

```text
python -m kernel init /path/to/project
```

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
