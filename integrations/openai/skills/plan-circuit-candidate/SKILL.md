---
name: plan-circuit-candidate
description: Plan a Sigilicon circuit Candidate when the user wants to turn design intent into a Design Brief, select an existing owner target operation, identify evidence needs, or prepare a bounded proposal without running EDA or changing canonical source.
---

# Plan a circuit Candidate

Produce a reviewable proposal whose assumptions, source identities, and required evidence are explicit. The result is a plan, not a persisted Candidate or an engineering pass.

## Workflow

1. Load the repository `AGENTS.md`, the closest owner rules, and the canonical spec links available for the selected target. Keep IP/PDK facts in those owned sources rather than copying them into this workflow. Check: governing rule and spec identities are recorded.
2. Call `project.inspect` for the requested owner. Check: the owner, source commit/dirty state, canonical target/spec links, and current capability status are exact.
3. Draft a Design Brief with separate lists for intent, confirmed requirements, provisional assumptions, preferences, and unresolved questions. Place every unconfirmed numeric value under provisional assumptions. Check: no unconfirmed value appears as a requirement.
4. For an existing cataloged target operation, call `target.plan` with `owner`, `target`, and `operation`. Bind the Candidate Plan to the returned immutable `plan_identity` and list required capabilities, policies, stage artifacts, and stop conditions. Do not invent a target operation or bypass its owner catalog. Check: the plan identity resolves to exactly that owner/target/operation.
5. Use `run.inspect` only for an exact prior run supplied as input evidence. Separate diagnostic/regression observations from qualification/signoff evidence and preserve missing, unsupported, backend-unavailable, violated, and identity-mismatch outcomes. Check: every observation retains its run identity and evidence role.
6. If exact canonical Candidate and stage artifacts already exist, call `candidate.validate`; bind any further discussion to the returned Candidate identity. Validation proves schema and lineage, not circuit performance. Check: the validated Candidate identity is unchanged in the proposal.
7. Propose the smallest bounded next evaluation and its expected artifact lineage. Leave canonical source unchanged and mark promotion as requiring a human-approved Promotion Plan. Check: the output is proposal-only and names a budget and stop condition.
8. If the user explicitly requests a bounded campaign, use the independent `sigilicon campaign plan/run` CLI only after owner opt-in; this skill never calls a campaign by default. Check: no campaign operation is proposed without that explicit request.

## Stop conditions

- Ask for the owner or canonical requirement when alternatives would change topology, sizing scope, evidence role, or acceptance policy.
- Stop if the selected target operation cannot be resolved, its immutable plan identity changes, or required capability/spec evidence is unavailable.
- Stop before target execution, OA mutation, product qualification, or source promotion.

## Output

Return a Design Brief draft, owner/target/operation and source identities, immutable
plan identity, Candidate stages, required evidence matrix, bounded budget/stop
conditions, open decisions, and the next permitted tool call. Label the entire
result `proposal-only` and include no new qualification threshold.
