---
name: inspect-design-status
description: Inspect a Sigilicon circuit or physical-design target when the user asks for its owner, canonical spec, source status, available Flow targets, existing run evidence, blockers, or current engineering conclusion.
---

# Inspect design status

Produce an identity-backed status report without executing a Flow or treating a model summary as engineering evidence.

## Workflow

1. Load the repository `AGENTS.md` and the closest owner rules available in the workspace context. Treat a missing rule or canonical specification as a reported gap, not permission to infer it.
2. Call `project.inspect`, narrowed to the requested owner when known. The step is complete when the owner resolves exactly and the response identifies its source commit/dirty state, cataloged targets, and explicit capability status.
3. If the user supplied a Flow identity but no run identity, call `flow.plan` only to report the typed DAG, required capabilities, policies, and pre-execution blockers. Keep its conclusion at `planned`.
4. If the user supplied a complete owner/Flow/target/run identity, call `run.inspect`. Report only the recorded status, node results, policies, artifacts, and facts returned for that exact identity.
5. Classify every claim by authority: source contract, plan, recorded Flow result, or non-conclusion. A DRC/LVS/PEX/qualification/signoff pass exists only when the returned exact evidence and canonical policy establish it.

## Stop conditions

- Stop and ask for the missing identity when owner, Flow, target, and run cannot be selected unambiguously.
- Stop on identity drift, cross-owner rejection, unsupported target, missing evidence, or backend-unavailable status. Preserve the returned non-conclusion.
- Stop before any execution, cancellation, OA mutation, or source promotion; this workflow is read-only.

## Output

Return the owner and source identity, canonical target/spec links, recorded evidence by role/level/scope, unmet conditions, and the next permitted action. State whether the worktree is dirty and distinguish observed status from product qualification.
