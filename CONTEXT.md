# Project Kernel

This context names the project provenance shared by people and agents.

## Language

**State Tree**:
The project-local collection of immutable payloads and hash-chained facts recorded about work.
_Avoid_: Database, workflow engine

**Snapshot**:
A complete immutable description of project state at one ledger point.
_Avoid_: Cache, mutable state file

**Patch**:
A declared set of operations that derives one Snapshot from a specific parent Snapshot.
_Avoid_: Event, command

**State Head**:
The reference to the current Snapshot accepted by the State Tree.
_Avoid_: Latest cache, working copy

**Schema**:
A durable constraint defining which Snapshots may be accepted while it is in force.
_Avoid_: Validator, state model

**Schema Head**:
The reference to the Schema currently governing accepted Snapshots.
_Avoid_: Current validator, schema cache

**Write Contract**:
A durable actor-specific grant defining which Snapshot paths may be read as Patch preconditions or written.
_Avoid_: Prompt permission, role description

**Contract Head**:
The reference to the Write Contract currently governing Patch authority.
_Avoid_: Permission cache, current role

**View**:
A deterministic actor-specific subdocument of a Snapshot, constrained by its Write Contract and Schema.
_Avoid_: Slice, prompt context

**Collection**:
A separately retained sequence referenced by a Snapshot rather than embedded in it.
_Avoid_: Inline array, project history

**Verification Checkpoint**:
A disposable claim that a ledger prefix has already passed integrity verification.
_Avoid_: Source of truth, ledger head

**Portable History**:
The durable portion of a State Tree that retains its meaning when copied, cloned, or versioned.
_Avoid_: Runtime state, cache

**Runtime State**:
Ephemeral coordination data needed by a running writer. It is neither provenance nor Portable History.
_Avoid_: Ledger, project history
