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
A constraint within a Blueprint defining which Snapshots may be accepted.
_Avoid_: Validator, state model

**Write Contract**:
A Blueprint's actor-specific grant defining which Snapshot paths may be read as Patch preconditions or written.
_Avoid_: Prompt permission, role description

**Blueprint**:
The indivisible authority document binding one Schema to its Write Contracts.
_Avoid_: Workflow, prompt, separate policy bundle

**Blueprint Head**:
The reference to the Blueprint currently governing Snapshot acceptance and Patch authority.
_Avoid_: Schema Head, Contract Head, policy cache

**Meta-schema**:
The structural contract a Blueprint must satisfy before its authority can be considered.
_Avoid_: Project Schema, model prompt

**Workflow Rule**:
A Blueprint declaration relating one accepted Patch event to an actor that may respond.
_Avoid_: Job, dispatch command, agent invocation

**Circuit Policy**:
A Blueprint's thresholds for interpreting refusal and repetition signals.
_Avoid_: Retry loop, watcher, execution policy

**Rejection Record**:
A durable fact preserving an attributable Patch refusal without advancing the State Head.
_Avoid_: Error log, failed Snapshot, discarded attempt

**Circuit Verdict**:
A deterministic advisory judgment derived from accepted and rejected ledger facts.
_Avoid_: Dispatch decision, command, mutable status

**Schedule**:
The actor responses implied by Workflow Rules for one accepted Patch.
_Avoid_: Queue, running workflow, background job

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
