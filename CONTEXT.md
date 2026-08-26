# Sigilicon EDA

Sigilicon provides reusable design facts and execution semantics across EDA projects, tools, technologies, and design styles.

## Language

**Physical Design Job**:
A self-contained physical design problem consisting of a normalized design, technology facts, constraints, and requested stages.
_Avoid_: Layout recipe, PDK job

**Physical Design Result**:
The complete outcome of a Physical Design Job, including solutions, constraint outcomes, diagnostics, metrics, and reproducibility identity.
_Avoid_: Successful process, generated layout

**Physical Design Model**:
A database-neutral description of hierarchy, physical masters, instances, ports, nets, and legal design regions.
_Avoid_: OA model, GDS model

**Technology Model**:
A normalized collection of manufacturing units, layers, vias, routing resources, and physical rules supplied by a technology owner.
_Avoid_: Hard-coded PDK

**Constraint**:
A named, checkable condition over physical design entities with explicit hardness and satisfaction status.
_Avoid_: Prompt, hint

**Placement Solution**:
Exact instance origins and orientations satisfying the supported placement constraints.
_Avoid_: Floorplan suggestion

**Routing Solution**:
Exact conductive geometry and vias connecting nets under the supported routing constraints.
_Avoid_: Connectivity suggestion

**Materialization Plan**:
An audited instruction set for writing an accepted physical design solution into a persistent layout database.
_Avoid_: Physical Design Result
