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

**Technology Capability**:
A routing or rule capability derived from facts present in a Technology Model and checked against a Physical Design Job requirement.
_Avoid_: Tool option, assumed feature

**Routing Resource**:
A track pattern or gridless region in which conductive geometry may be considered on a routing layer.
_Avoid_: Route, wire

**Via Definition**:
Local lower-conductor, cut, and upper-conductor geometry for one legal inter-layer connection.
_Avoid_: Via name string

**Via Stack**:
An ordered, layer-continuous sequence of Via Definitions spanning more than one adjacent-layer transition.
_Avoid_: Independent via list

**Physical Rule**:
A typed, checkable geometric requirement supplied by a technology owner.
_Avoid_: Rule-deck text, magic number

**Obstruction**:
Master-owned layer geometry that routing must not occupy after applying the instance placement transform.
_Avoid_: Keep-away hint

**Constraint**:
A named, checkable condition over physical design entities with explicit hardness and satisfaction status.
_Avoid_: Prompt, hint

**Alignment Constraint**:
A condition requiring selected geometric anchors of multiple instances to share one coordinate along an axis.
_Avoid_: Same row

**Ordering Constraint**:
A directional condition requiring one instance bounding box to precede another along an axis with an optional gap.
_Avoid_: Signal flow hint

**Symmetry Constraint**:
A condition requiring paired instance bounding boxes to be geometric reflections across an explicit axis coordinate.
_Avoid_: Matching pair

**Array Constraint**:
A condition assigning an ordered set of instance origins to a row-major grid with explicit pitches.
_Avoid_: Repetition hint

**Separation Constraint**:
A condition requiring two instance bounding boxes to maintain a minimum gap along a selected axis or along either axis.
_Avoid_: Keep away

**Placement Objective**:
A named, weighted measure used to rank legal Placement Solutions without changing hard legality.
_Avoid_: Qualification metric, signoff target

**Pin Access**:
Master-owned layer geometry through which a placed instance terminal may be connected.
_Avoid_: Pin center estimate

**Placement Solution**:
Exact instance origins and orientations satisfying the supported placement constraints.
_Avoid_: Floorplan suggestion

**Routing Solution**:
Exact conductive geometry and vias connecting nets under the supported routing constraints.
_Avoid_: Connectivity suggestion

**Materialization Plan**:
An audited instruction set for writing an accepted physical design solution into a persistent layout database.
_Avoid_: Physical Design Result
