---
status: accepted
---

# Keep agent integration native, clean-room, and client-neutral

Sigilicon will expose its own public Modules through a standards-compliant native MCP server and focused workflow Skills without importing, wrapping, translating, or emulating PANDA code, prompts, handlers, JSON contracts, binaries, or environment conventions. MCP, CLI, and Python remain peer clients of the same Sigilicon Interface, so deleting the MCP integration removes protocol access but no domain behavior; this trades compatibility shortcuts for auditable ownership and prevents a second circuit-design implementation from forming in protocol handlers.

## 2026-08-31 final boundary

The default MCP surface is limited to `project.inspect`, `target.plan`,
`target.run`, `run.inspect`, `run.cancel`, `candidate.validate`, and
`candidate.promotion_plan`. Target requests carry `owner`, `target`, and
`operation`; a run carries an immutable `plan_identity`. Run resources use the
`owner/target/operation/run` hierarchy. Bounded campaigns are not MCP tools and
require an explicit `sigilicon campaign plan/run` CLI request.
OA-XStream and combined XStream-Calibre are explicit owner opt-ins. No compatibility protocol or
legacy selection alias is provided.
