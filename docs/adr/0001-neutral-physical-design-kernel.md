---
status: accepted
---

# Keep physical-design solving neutral and separate from materialization

Sigilicon will own a technology-, design-style-, and database-neutral Physical Design Job → Physical Design Result kernel. Project PDK facts and design intent are normalized before this seam, while OA, DEF, GDS, and other persistent representations are handled after it; the existing materialization plan therefore remains downstream instead of becoming the solver model. This trades some adapter work for a reusable kernel whose Interface cannot be shaped by one project, PDK, generator, or EDA database.

## 2026-08-31 final boundary

The stable kernel contains only the complete canonical `PhysicalDesignJob`, its
tool-independent `PhysicalDesignResult`, and the downstream Materialization Plan
and Materialization Receipt contracts. The Job identity is the digest of the full
stable canonical Job; the Result does not contain owner policy, repair, execution
lineage, or verification conclusions. Sigilicon provides no built-in placement,
routing, or physical-closure implementation. A project recipe selects an external
Adapter explicitly. OA-XStream materialization and XStream-Calibre verification
remain downstream implementations and never become solver policy. The seam has no
compatibility alias, reference fallback, or legacy result interpretation.
