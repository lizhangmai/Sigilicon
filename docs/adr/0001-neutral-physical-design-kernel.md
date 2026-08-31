---
status: accepted
---

# Keep physical-design solving neutral and separate from materialization

Sigilicon will own a technology-, design-style-, and database-neutral Physical Design Job → Physical Design Result kernel. Project PDK facts and design intent are normalized before this seam, while OA, DEF, GDS, and other persistent representations are handled after it; the existing materialization plan therefore remains downstream instead of becoming the solver model. This trades some adapter work for a reusable kernel whose Interface cannot be shaped by one project, PDK, generator, or EDA database.

## 2026-08-31 final boundary

The stable kernel contains only the complete canonical `PhysicalDesignJob`, its
tool-independent `PhysicalDesignResult`, and the downstream Materialization Plan
and Materialization Receipt contracts. The Job identity is the digest of the full
stable canonical Job; the Result does not contain closure evidence, owner policy,
repair, or execution lineage. Reference PNR, closure/repair, OA-XStream, and
combined XStream-Calibre are explicit `sigilicon.experimental`/owner opt-ins on a
target/operation, never default MCP capabilities. The boundary has no compatibility
alias or legacy result interpretation.
