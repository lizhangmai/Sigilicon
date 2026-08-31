---
name: close-physical-design
description: Plan or execute a Sigilicon physical-design target operation when the user asks for P&R, materialization, DRC/LVS, PEX, post-layout feedback, bounded repair, or the exact closure status of a Candidate.
---

# Close physical design

Drive only cataloged, identity-bound physical work and preserve every non-conclusion. A successful process exit or DRC-clean result alone is not whole-design closure.

## Workflow

1. Load the repository and closest owner `AGENTS.md`, the selected physical target, platform contracts, canonical qualification specification, and OA safety rules. Do not discover a neighboring IP workflow by convention. Check: the owner, target, operation, and governing contracts are identified.
2. Call `project.inspect` for the owner, then `target.plan` for the exact cataloged `owner`/`target`/`operation`. Record the immutable plan identity, typed topology, policies, required capabilities, platform asset identities, and mutation level. Check: the plan resolves exactly and has no profile or registry fallback.
3. Before execution, verify the user-approved scope and budgets cover every planned node. For `mutate-workspace`, require the launcher grant plus the existing managed scratch target, exact OA lease, mutation scope, and receipt checks. Never accept a command, path, environment, or Adapter selector from a model call. Check: each requested capability and mutation guard is granted.
4. Call `target.run` only with the approved immutable `plan_identity` and bounded node/time budget. Retain the durable run identity. Use `run.cancel` only for that principal-bound live run when requested or when the workflow’s stop condition fires. Check: the run belongs to the same owner/target/operation and no plan is re-resolved during execution.
5. Call `run.inspect` and evaluate stages in order: deterministic physical result, materialization receipt, DRC, LVS, PEX, post-layout evidence, then qualification. Require exact parent and subject identities at every edge. Check: each stage is `satisfied`, `violated`, `unsupported`, or an explicit non-conclusion.
6. If a stage is violated and typed attribution is available, prepare the smallest owner-policy repair proposal. Continue only after the deterministic closure repair compiler accepts it and a new parent-bound target plan is produced; rerun all owner-required regressions. Check: repair acceptance and the new immutable plan identity are recorded.
7. Reference PNR, closure/repair, OA-XStream, and combined XStream-Calibre work is `sigilicon.experimental`/owner opt-in. If the user explicitly requests a bounded campaign, use only `sigilicon experimental campaign plan/run`; this target workflow does not call a campaign by default. Check: experimental scope and explicit request are recorded before use.
8. Stop when closure is established by every requested stage, or when a distinct terminal status, budget, or unsupported repair prevents further progress. Check: the final report names the terminal status and next permitted action.

## Stop conditions

- Stop before execution if the immutable plan identity changes, the grant lacks a required capability, or platform/source identity drifts.
- Stop on a missing or non-materialized receipt; do not run downstream verification against an unbound layout.
- Keep DRC clean, LVS clean, PEX complete, post-layout satisfied, and qualification passed as separate claims. Missing later stages remain explicit non-conclusions.
- Stop before canonical source promotion. A closed derived Candidate still requires a human-approved Promotion Plan and ordinary Git review.

## Output

Return the owner/target/operation, immutable plan and run identities, requested
scope and budgets, per-stage status and evidence hashes, OA/materialization receipt
identity when applicable, attributed blockers and accepted repair identity, consumed
iterations/cost, product conclusion authority, and the next permitted action.
Include no raw EDA command, site path, secret, or new threshold.
