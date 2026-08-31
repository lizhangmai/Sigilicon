---
status: accepted
---

# Keep whole-design campaigns above the deterministic Flow engine

FlowEngine will continue to resolve and execute one deterministic typed DAG, while a separate Design Campaign Module owns bounded cross-attempt progression over topology, sizing, pre-layout, physical, extracted, and qualification evidence. Stage-specific repair compilers, not the campaign or an LLM, validate and compile proposed changes. The campaign never turns FlowEngine into a stateful agent loop and never recovers state from reports or conversation text.

## 2026-08-30 implementation boundary and migration

Campaign continuation data crosses the deterministic executor only as an opaque
portable extension.
`FlowEngine` validates the extension name declared by the selected Action and
Adapter, preserves it in plan/action-request records, and does not import,
decode, or validate the Campaign payload type.

This boundary intentionally removes the campaign-specific executor model API:

- `ActionContract.accepts_design_campaign_iteration` becomes
  `accepted_extensions=(DESIGN_CAMPAIGN_ITERATION_EXTENSION,)`;
- Adapter `accepts_design_campaign_iteration` becomes the corresponding
  `accepted_extensions` tuple;
- `FlowNode.design_campaign_iteration` becomes `FlowNode.extensions`;
- adapters read `ActionContext.extensions` and use
  `design_campaign_iteration_input()` to decode the Campaign-owned payload;
- `DesignCampaignIterationInput` is owned by the campaign module;
  the former higher-level workflow import is removed because the package
  dependency matrix forbids Flow from importing a higher-level workflow.

Derived Campaign plan and action-request records keep the existing top-level
`design_campaign_iteration` payload field. Ordinary execution records no longer
emit that field with a null value. Because exact plan records are authorization
identities, approvals or replay records created before this change must be
regenerated rather than silently treated as equivalent.

## 2026-08-31 final boundary

The stable API names one target operation at a time: `target.plan` resolves an
owner/target/operation and returns an immutable `plan_identity`; `target.run`
executes only that plan identity. Design campaigns remain bounded orchestration
and are available only through the explicit
`sigilicon campaign plan/run` CLI or an owner opt-in, never the
default MCP inventory. OA-XStream and combined XStream-Calibre remain explicitly
selected owner implementations. The migration is destructive: no compatibility
alias, profile fallback, or old selection record is interpreted as a
target/operation plan.
