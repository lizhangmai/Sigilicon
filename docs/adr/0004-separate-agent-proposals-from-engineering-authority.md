---
status: accepted
---

# Separate agent proposals from engineering authority

LLMs may produce Design Briefs, proposals, attribution, and next-step suggestions, but only identity-bound evidence from an executed, authorized backend and an owner-authored policy may support DRC, LVS, PEX, qualification, or signoff conclusions. Design Candidates remain derived artifacts by default, and promotion into canonical Git source always requires a non-mutating Promotion Plan plus explicit human approval; this preserves human control and fail-closed evidence semantics at the cost of forbidding autonomous source promotion.

## 2026-08-31 final boundary

Agent proposals may select an owner `target` and `operation` and may reference the
immutable `plan_identity` returned by `target.plan`, but they cannot execute
`target.run`, assert a conclusion, or mutate source without the granted interface
and owner policy. Campaign continuation is an explicit campaign CLI action,
never a default MCP operation. OA-XStream and
combined XStream-Calibre require explicit owner opt-in; no compatibility alias
widens proposal authority.
