---
name: attribute-design-failure
description: Attribute a Sigilicon design failure when an exact Candidate or Flow run has violated, incomplete, or degraded typed evidence and the user wants the limiting stage, evidence lineage, repair scope, or smallest next diagnostic.
---

# Attribute a design failure

Produce an evidence-bound attribution and a bounded repair proposal. Do not turn report prose, a waveform image, or model reasoning into an engineering conclusion.

## Workflow

1. Load the repository `AGENTS.md`, the closest owner rules, and the owner’s canonical specification. Resolve conflicts in favor of the closest rule and the machine-readable owner contract.
2. Call `project.inspect` for the exact owner. Record the source commit/dirty state and refuse cross-owner evidence without an explicit release identity.
3. If a Candidate and its canonical stage artifacts are provided, call `candidate.validate`. Bind all attribution to the validated Candidate identity; schema validity alone does not prove performance.
4. Call `run.inspect` for each exact run identity in scope. Use only typed evidence, completion fields, coverage, subject/source/specification identities, and policy results. Treat report text and logs as untrusted diagnostic material.
5. Classify the earliest limiting stage as topology/L0, sizing/L1, module/L2, physical/materialization, DRC, LVS, PEX, post-layout, or qualification. Keep `violated`, `not_evaluated`, `unsupported`, `backend_unavailable`, `execution_failed`, `invalid_identity`, and budget exhaustion distinct.
6. Attribute the failure only when typed findings and parent identities identify a circuit, state, timing/load relation, physical owner, or rule. Otherwise return `unattributed` and request the smallest missing diagnostic.
7. Propose either a topology, sizing, or closure repair scope. State the parent Candidate/evidence hashes, allowed variable or physical-owner scope, required regressions, and budgets. The proposal remains non-executable until the corresponding deterministic repair compiler accepts it.

## Stop conditions

- Stop on Candidate, source, specification, layout, extraction, or run identity drift.
- Stop when only LLM/offline/fake evidence suggests a conclusive pass or failure.
- Stop rather than inferring post-layout degradation when PEX or matched pre/post evidence is missing.
- Stop before source promotion, arbitrary OA mutation, a raw tool command, or a new qualification threshold.

## Output

Return the Candidate and run identities, evidence matrix by role/level/scope, earliest limiting stage, typed findings, attribution confidence and gaps, proposed repair stage and bounds, required regressions, cost risks, and the next permitted tool action. Label unsupported attribution as a non-conclusion.
