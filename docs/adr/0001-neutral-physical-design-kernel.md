---
status: accepted
---

# Keep physical-design solving neutral and separate from materialization

Sigilicon will own a technology-, design-style-, and database-neutral Physical Design Job → Physical Design Result kernel. Project PDK facts and design intent are normalized before this seam, while OA, DEF, GDS, and other persistent representations are handled after it; the existing materialization plan therefore remains downstream instead of becoming the solver model. This trades some adapter work for a reusable kernel whose Interface cannot be shaped by one project, PDK, generator, or EDA database.
