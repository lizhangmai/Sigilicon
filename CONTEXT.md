# Sigilicon EDA

Sigilicon provides reusable design facts and execution semantics across EDA projects, tools, technologies, and design styles.

## Language

**Physical Design Job**:
A self-contained physical design problem consisting of a normalized design, technology facts, constraints, and requested stages.
_Avoid_: Layout recipe, PDK job

**Physical Design Result**:
The complete outcome of a Physical Design Job, including solutions, constraint outcomes, diagnostics, metrics, typed Placement-Routing Closure Evidence, and reproducibility identity.
_Avoid_: Successful process, generated layout

**Physical Design Flow Artifact**:
A canonical, strictly validated Physical Design Job, Physical Design Result, or Placement-Routing Closure Evidence value transferred between managed Flow stages.
_Avoid_: Arbitrary JSON dump, Stage Report reconstruction

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

**Routing Resource Graph**:
The compiled set of gridless corridors, explicit tracks, layer segments, and via sites with their legal-use and capacity facts for one Routing Problem.
_Avoid_: Congestion bins, technology model wrapper

**Routing Resource Identity**:
A stable name for one capacity-bearing resource that is unchanged by route order or solver allocation.
_Avoid_: Geometry object identity, transient search state

**Routing Resource Demand**:
The capacity a Routing Solution claims from a Routing Resource, attributed to its owning net and route scope.
_Avoid_: Wire-length estimate, post-route metric

**Routing Capacity**:
The maximum simultaneous demand a Routing Resource can legally carry before negotiated routing must revise an attributed route.
_Avoid_: Congestion score, utilization metric

**Routing Conflict Set**:
A stable collection of attributed hard blockers, capacity overflows, unrouted branches, group failures, resource exhaustion, topology conflicts, or budget exhaustion preventing the current Routing State from closing.
_Avoid_: Error strings, failed-net list

**Routing Victim**:
A deterministically selected routed net or branch whose attributed scope may be removed to resolve a Routing Conflict Set.
_Avoid_: Random retry, global net permutation

**Routing Termination Evidence**:
The typed conclusion that negotiated routing closed, proved infeasible, found an unsupported capability, exhausted route states, or exhausted conflict-resolution iterations.
_Avoid_: Process exit status, diagnostic-code parsing

**Routing Placement Pressure**:
A typed attribution from a Routing Conflict Set and affected Routing Resources to stable Physical Owners that placement may legally repair, while preserving fixed, unsupported, and unowned evidence.
_Avoid_: Congestion message, global placement retry

**Physical Owner**:
A stable instance, instance pin, port, or top-level Routing Blockage identity responsible for placed obstruction or access geometry, with explicit movable, fixed, or unsupported repair status.
_Avoid_: Rectangle object identity, inferred blocker name

**Routing Blockage**:
A named top-level set of routing-obstruction shapes in a bounded local coordinate system, with an exact placement and optional local repair region; absence of a repair region makes the owner fixed.
_Avoid_: Anonymous rectangle, master instance invented only to carry an obstruction

**Placement Repair**:
A deterministic, local displacement of only Routing Placement Pressure-attributed movable instances or Routing Blockages while preserving placement legality, declared repair regions, and hard constraints.
_Avoid_: Fresh placement, random perturbation

**Placement Repair Problem**:
An immutable local choice set compiled from a Placement Solution and Routing Placement Pressure, including legal owner scope, predicted resource release, pin-access impact, displacement, rejection history, and a state budget.
_Avoid_: Global placement retry, candidate list in the closure coordinator

**Placement Repair State Budget**:
The maximum number of deterministic local placement candidates considered while compiling one Placement Repair Problem, independent of closure iteration count.
_Avoid_: Placement-Routing retry count, timeout

**Placement-Routing Closure**:
The bounded sequence of Placement Solution, compiled Routing Problem, negotiated Routing Solution, independent evaluation, Routing Placement Pressure, and Placement Repair that ends in closure or typed termination evidence.
_Avoid_: Router retry loop, flow coordinator

**Placement-Routing Closure Evidence**:
The public typed conclusion of Placement-Routing Closure, including distinct routing and outer-loop termination, final Routing Closure Quality, stable conflict summaries, Routing Placement Pressure summaries with owner mobility and attribution reason, and accepted or rejected repair provenance.
_Avoid_: Stage Report parsing, success boolean

**Routing Closure Quality**:
A typed, publicly observable closure state over resource overflow, unrouted branches, blocker and group failures, via and topology failures, physical-owner pressure, routed progress, independent evaluation, and placement displacement.
_Avoid_: Conflict count, Stage Report metric tuple

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
Layer geometry that routing must not occupy after applying its Physical Owner placement transform; it may be owned by a master instance or a top-level Routing Blockage.
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

**Routing Tree**:
The deterministic trunk-and-branch topology connecting every terminal of one multi-terminal net while preserving shared conductor ownership.
_Avoid_: Pairwise route list, Steiner estimate

**Route Branch**:
A stable terminal-to-tree portion of a Routing Tree with attributed geometry and Routing Resource Demand; a leaf branch is locally revisable only when no collective constraint depends on its topology.
_Avoid_: Arbitrary segment slice, whole net

**Route Segment**:
An exact-width, axis-aligned conductor between two manufacturing-grid points on one routing layer and owned by one net.
_Avoid_: Path hint, centerline only

**Route Via**:
An occurrence of a Via Definition at an exact manufacturing-grid origin and owned by one net.
_Avoid_: Layer switch, via name without geometry

**Routing Search Budget**:
The maximum number of deterministic route states a P&R Execution Policy permits before returning `exhausted` instead of claiming infeasibility.
_Avoid_: Timeout, failed route

**Routing Iteration Budget**:
The maximum number of deterministic conflict-resolution rounds a P&R Execution Policy allows while routing selectively revises affected nets.
_Avoid_: Retry timeout, unbounded rip-up

**Routing Group Policy**:
A compiled collective policy for related nets that names length matching, shielding, route-order dependencies, reroute scope, and closure costs.
_Avoid_: Coupled-constraint flag, special net class

**Routing State**:
The changing partial Routing Solution, occupancy, and accumulated conflict history used during one negotiated routing run, separate from the static Physical Design Job.
_Avoid_: Routing Problem, Stage Report metrics

**Negotiated Routing**:
Deterministic routing that attributes a conflict, raises its historical cost, and revises only the affected net or Routing Group.
_Avoid_: Full net-order permutation, unbounded retry

**P&R Execution Policy**:
Reference-engine search budgets and analysis resolution recorded independently from physical-design intent.
_Avoid_: Design constraint, technology rule

**Routing Congestion Bin**:
A deterministic die partition used to estimate per-layer, per-direction route demand, track-equivalent capacity, and overflow without replacing exact geometry checks.
_Avoid_: DRC region, signoff congestion result

**Routing Constraint**:
A named, typed condition over one net's accepted Routing Solution, separate from Placement Constraints and reported through the same explicit constraint outcome states.
_Avoid_: Router hint, placement constraint

**Routing Layer Constraint**:
A Routing Constraint limiting one net's conductive path to an explicit set of routing layers.
_Avoid_: Preferred layer guess

**Routing Region Constraint**:
A Routing Constraint requiring the primary path between a net's first two terminals to visit every named layer region in declared order.
_Avoid_: Placement fence, vague waypoint hint

**Routing Length Constraint**:
A Routing Constraint defining an inclusive DBU length range over the exact Route Segments of one net.
_Avoid_: Timing constraint, estimated HPWL

**Routing Via Count Constraint**:
A Routing Constraint placing an inclusive upper bound on the Via Definition occurrences used by one net.
_Avoid_: Via cost

**Routing Skew Constraint**:
A Routing Constraint placing an inclusive upper bound on the difference between the longest and shortest exact Route Segment lengths in a named net group.
_Avoid_: Clock uncertainty, estimated HPWL difference

**Routing Shield Constraint**:
A Routing Constraint requiring every selected signal Route Segment and layer transition to have continuous nearby coverage from a connected named shield route, including a corresponding shield via.
_Avoid_: Net-name convention, proximity hint

**Routing Solution Check**:
An independent reconstruction of connectivity, resource coverage, obstruction clearance, width, spacing, and via-rule compliance from a Physical Design Job and returned Routing Solution.
_Avoid_: Solver success flag, signoff DRC/LVS

**Materialization Plan**:
An audited instruction set for writing an accepted physical design solution into a persistent layout database.
_Avoid_: Physical Design Result
