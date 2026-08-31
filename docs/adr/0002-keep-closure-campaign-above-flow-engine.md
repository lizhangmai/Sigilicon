---
status: accepted
---

# Keep multi-round closure above the deterministic Flow engine

FlowEngine will continue to plan and execute one deterministic typed DAG. A separate Closure Campaign Module owns bounded attempts, typed quality comparison, artifact provenance, attributed feedback, and independent state and iteration budgets. Each campaign attempt is an explicit resolved owner/target/operation plan, so stage execution stays reusable and loop policy cannot recover state from report strings or mutate the executor into a physical-design coordinator. This adds one workflow-level orchestration Module, but preserves a deep FlowEngine Interface and keeps future DRC/LVS, PEX, post-layout, and qualification evidence independently owned.

## 2026-08-31 final boundary

The stable public execution seam is `target.plan` followed by `target.run`, selected
by explicit `owner`, `target`, and `operation`; execution accepts only the immutable
`plan_identity`. A bounded closure campaign is experimental and is reachable only
through `sigilicon experimental campaign plan/run` or an explicit owner extension,
not through default MCP. Closure/repair, reference PNR, OA-XStream, and combined
XStream-Calibre remain opt-in. No compatibility alias preserves the former
selection surface.
