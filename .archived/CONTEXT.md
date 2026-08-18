# Project Kernel

This context describes the provenance shared by people and agents working in one project.

## Language

**State Tree**:
The project-local collection of recorded immutable payloads and the hash-chained facts that reference them.
_Avoid_: Database, workflow state

**Portable History**:
The durable portion of a State Tree that retains its meaning when copied, cloned, or versioned.
_Avoid_: Runtime state, cache

**Runtime State**:
Ephemeral coordination data needed by a running writer. It is neither provenance nor part of Portable History.
_Avoid_: Ledger, project history
