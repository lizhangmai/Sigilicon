---
status: accepted
---

# Keep physical interchange neutral and limited to materialization

Sigilicon owns a technology-, design-style-, and database-neutral Physical Design Job → Physical Design Result interchange contract. Project tools produce the pair; OA, GDS, and other persistent representations are handled by the downstream materialization plan. Standard ASIC implementation retains its own domain Action rather than being flattened into a second generic solver abstraction.

## 2026-08-31 final boundary

The stable interchange contains only the canonical design/technology geometry,
its materializable placements and routes, and the downstream Materialization Plan
and Materialization Receipt contracts. The Result does not contain constraints,
objectives, stage diagnostics, owner policy, repair lineage, or verification
conclusions. Sigilicon provides no generic solve Action or built-in placement,
routing, or physical-closure implementation. OA-XStream materialization and
XStream-Calibre verification remain downstream implementations.
