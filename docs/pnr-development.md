# General P&R development baseline

Sigilicon owns a reusable physical-design kernel. Its canonical Interface is:

```python
result = run(job)
```

The `PhysicalDesignJob` contains a normalized design, normalized technology facts,
named constraints, requested stages, and a separate `PnrExecutionPolicy`. Physical
intent and reference-engine search policy have independent provenance identities.
The `PhysicalDesignResult` contains only observable solver outcomes: exact solutions,
constraint status, diagnostics, metrics, and deterministic provenance. The kernel
performs no file discovery, database mutation, EDA invocation, or project-specific
interpretation.

## Product scope

The kernel must remain independent of:

- process vendor, node, and PDK;
- standard-cell, custom, analog, array, or mixed-signal design style;
- OA, LEF/DEF, GDS, OASIS, and generator object models;
- project and IP ownership;
- signoff vendor and execution environment.

Technology and design owners normalize source facts before the kernel seam.
Output adapters lower accepted results into a Materialization Plan or another
persistent representation after the seam. Independent DRC, LVS, PEX, and
post-layout analysis remain authoritative.

## PANDA influence

The architecture borrows four public ideas from
[PANDA](https://github.com/PKU-IDEA/PANDA/tree/9e53e43c0d29bdc8c0f326640b9d66c7075e841b):

1. separate intent and constraint planning from deterministic geometry solving;
2. solve against real physical master geometry and terminal access rather than
   guessed rectangles;
3. keep placement, routing, materialization, and signoff as distinct stages;
4. feed independent physical and electrical verification results back into later
   design iterations.

Sigilicon does not consume or reproduce PANDA's opaque kernels. Algorithms,
models, tests, and provenance are developed independently against Sigilicon's
general contract.

## Capability rule

Every requested stage and constraint has one of four observable outcomes:
`satisfied`, `violated`, `unsupported`, or `not_evaluated`. A normal process exit
is never treated as physical completion. Unsupported capabilities fail explicitly
and produce no candidate solution that could be mistaken for a completed result.

## Milestones

### M0 — neutral reference placement

Status: implemented.

- immutable Design, Technology, Constraint, Job, and Result values;
- integer DBU geometry and manufacturing-grid validation;
- fixed and movable rectangular masters with legal orientations;
- hard fence and minimum instance spacing enforcement;
- deterministic bottom-left reference placement;
- infeasibility and unsupported-capability diagnostics;
- canonical serialization and reproducible input identity;
- execution-policy identity separate from physical-design intent;
- neutral tests that exercise more than one technology model.

M0 is a contract and correctness reference. It is not an optimizing placer.

### M1 — general placement constraint system

Status: reference implementation complete.

- typed alignment, ordering, symmetry, array, separation, and region constraints;
- deterministic constraint-aware search with explicit search exhaustion;
- hard-constraint conflict reporting;
- soft objectives for area, wire length, density, and congestion;
- legality evaluation separated from weighted search inside the implementation;
- benchmark corpus covering unrelated design styles and technology grids.

### M2 — routable physical library and technology rules

Status: normalized technology and master-geometry foundation implemented and
consumed by the first reference router.

- transformed obstruction and terminal-access geometry;
- routing tracks and gridless resources;
- via definitions and stacks;
- typed width, spacing, enclosure, extension, and cut rules;
- capability negotiation for rules that a technology model cannot express.

### M3 — general routing

Status: deterministic track and gridless detailed-routing reference implemented;
general routing remains in progress.

- exact multi-layer Manhattan Route Segments over track and gridless Routing
  Resources;
- strict track-axis movement and legal orthogonal-layer Via intersections;
- transformed Pin Access and Obstruction consumption;
- conservative minimum-width and minimum-spacing enforcement;
- deterministic multi-terminal tree construction and inter-net spacing;
- legal Via Definition occurrences with cut spacing and two-sided enclosure checks;
- configurable die-bin congestion analysis with layer/direction demand, derived
  track-equivalent capacity, overflow metrics, and demand-aware path costs;
- typed layer, required-region topology, length-range, maximum-via, multi-net skew,
  and continuous parallel shielding Routing Constraints with structural validation,
  search-time topology/layer/via/objective filtering, and independent result
  outcomes;
- deterministic negotiated routing that attributes dynamic blockers, accumulates
  historical congestion cost, and rips up only the affected net or compiled
  Routing Group;
- independent state and iteration budgets, with partial legal routes retained for
  infeasible, unsupported, and exhausted results;
- independent geometry, spacing, resource-coverage, and connectivity checks over
  each returned Routing Solution;
- explicit distinction between infeasibility, capability gaps, and search-budget
  exhaustion;
- partial-stage observability: a successful Placement Solution remains available
  when the requested Routing Solution cannot be completed.

The current reference router deliberately rejects track problems whose terminals
have no legal track access and layer transitions whose Via Definitions lack complete
enclosure or cut-spacing facts.
Those are capability boundaries, not implicit fallbacks. The next routing increments
add active topology, length-matching, and via-shielding closure over the compiled
Routing Group policy.

- global routing and congestion estimation;
- detailed path and via construction;
- multi-terminal connectivity, rip-up, and negotiated congestion;
- layer, width, topology, length, skew, and shielding constraints;
- connectivity and rule checks over the returned solution.

### M4 — real adapters

Add an external seam only when at least two real adapters justify it. Qualification
requires at least two source formats, two output formats, two technology models,
and unrelated design styles using the same kernel without source edits.

### M5 — independent closure feedback

- DRC/LVS result ingestion without weakening signoff authority;
- PEX and post-layout metric correlation;
- iteration provenance connecting a result to its job and verification evidence;
- deterministic regression and benchmark reporting.
