# Project Kernel

A small, local coordination kernel for human-approved work by coding agents.

The repository contains no legacy ledger, compatibility layer, hosted workflow, model runner, or scheduler. Each project owns its own state tree; model-specific invocation remains in external Codex, Claude, DeepSeek, or GLM skills.

## Initialize a project

```text
state-tree init /path/to/project
```

This creates the local kernel boundary:

```text
.state-tree/
├── kernel.json             # accepted-state and durable-ledger pointers
├── kernel.lock             # serializes concurrent kernel writes
└── objects/
    └── sha256/             # immutable, content-addressed objects
```

Initialization stores a canonical empty accepted-state object. Repeating the command against a valid state tree is a safe no-op; it never replaces existing state.

For development before installing the console command:

```text
python -m kernel init /path/to/project
```

## Exchange artifacts

Artifact recording stores project files as immutable objects and appends genesis-rooted ledger entries. Only files inside the project and outside `.state-tree/` are accepted. Another agent retrieves the bytes using the returned SHA-256 reference.

## Local MCP server

`kernel-mcp` exposes three tools over stdio:

- `kernel_status`
- `submit_artifact`
- `read_artifact`

The project and actor identity are fixed when the process starts and are not tool arguments:

```text
kernel-mcp --project /path/to/project --actor codex
```

The project must already contain a valid `.state-tree/`. Example project-scoped Codex configuration:

```toml
[mcp_servers.kernel]
command = "/Users/osika/kernel/.venv/bin/kernel-mcp"
args = ["--project", "/absolute/path/to/project", "--actor", "codex"]
default_tools_approval_mode = "writes"
```

This binds attribution at the MCP boundary; it is not an operating-system security boundary. A runner that must be unable to open `.state-tree/` directly still requires filesystem sandboxing.

## Test

```text
python -m unittest discover -s tests -v
```
