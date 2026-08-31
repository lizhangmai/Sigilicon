---
name: inspect-design-status
description: Inspect a Sigilicon circuit or physical-design target when the user asks for its owner, canonical spec, source status, available target operations, existing run evidence, blockers, or current engineering conclusion.
---

# Inspect design status

Produce an identity-backed status report without executing a target operation or treating a model summary as engineering evidence.

## Workflow

1. Load the repository `AGENTS.md` and the closest owner rules available in the workspace context. Treat a missing rule or canonical specification as a reported gap, not permission to infer it. Check: the governing owner rule and spec identity are recorded.
2. Call `project.inspect`, narrowed to the requested owner when known. Check: the owner resolves exactly and the response identifies its source commit/dirty state, cataloged targets, and explicit capability status.
3. If the user supplied `owner`, `target`, and `operation` but no run identity, call `target.plan` only to report required capabilities, policies, and pre-execution blockers. Keep its conclusion at `planned`. Check: the immutable `plan_identity` is recorded and no execution occurred.
4. If the user supplied a complete `owner`/`target`/`operation`/run identity, call `run.inspect`. Report only the recorded status, node results, policies, artifacts, and facts returned for that exact identity. Check: every returned record matches all four identity components.
5. Classify every claim by authority: source contract, target plan, recorded run result, or non-conclusion. A DRC/LVS/PEX/qualification/signoff pass exists only when the returned exact evidence and canonical policy establish it. Check: each claim has one authority label.
6. If the user explicitly requests a bounded experimental campaign, use only the independent `sigilicon experimental campaign plan/run` CLI after confirming owner opt-in; this read-only skill does not invoke campaigns by default. Check: no campaign call appears without that explicit request.

## Stop conditions

- Stop and ask for the missing identity when owner, target, operation, plan, or run cannot be selected unambiguously.
- Stop on identity drift, cross-owner rejection, unsupported target operation, missing evidence, or backend-unavailable status. Preserve the returned non-conclusion.
- Stop before any execution, cancellation, OA mutation, or source promotion; this workflow is read-only.

## Output

Return the owner/target/operation and source identity, immutable plan/run identities,
canonical target/spec links, recorded evidence by role/level/scope, unmet conditions,
and the next permitted action. State whether the worktree is dirty and distinguish
observed status from product qualification.
