# General P&R development baseline

Sigilicon owns a small, reusable physical-design kernel and two downstream
materialization records. Its stable solver Interface is:

```python
result = run(job)
```

The `PhysicalDesignJob` is a complete stable input: normalized design, normalized
technology facts, named constraints, and requested stages. `job_identity` is the
digest of that complete stable canonical serialization; a digest of only physical
intent, execution policy, or a partial source closure is not a job identity. The
`PhysicalDesignResult` contains only tool-independent solver observations: exact
solutions, constraint status, diagnostics, metrics, and the terminal capability
outcome. It contains no closure evidence, owner policy, repair plan, execution
lineage, or tool/database metadata.

`MaterializationPlan` is the database-neutral lowering of one stable job/result
pair, and `MaterializationReceipt` records the exact plan/result identity, target,
backend completion, and published artifact identity. These records do not imply
DRC/LVS, qualification, or signoff. The stable kernel and these materialization
contracts perform no file discovery, database mutation, EDA invocation, or
project-specific interpretation.

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

## Explicit experimental extensions

The reference PNR implementation, closure/repair campaign, OA-XStream
materialization, and combined XStream-Calibre verification are explicit
`sigilicon.experimental`/owner opt-ins. They may consume the stable
Job/Result and Materialization Plan/Receipt, but must not add closure evidence,
policy, repair, or lineage fields to those stable types. An owner exposes one of
these extensions only through an explicitly selected target/operation; none is a
default MCP capability or an implicit fallback.

The reference solver gives its complete `ReferencePnrJob` a separate canonical
identity that includes execution policy and repair lineage. Its result and closure
evidence identities derive from that experimental identity, while the result
provenance still names the normalized stable Job identity. Thus two policies may
address the same physical intent, but they cannot alias the same execution result.

## Stable workflow integration

The tool-independent `physical-design.solve` Action consumes a canonical
`physical-design.job` artifact and emits a canonical `physical-design.result`.
An owner target/operation assembles an Adapter around this Action and reports
execution separately from the typed physical result: failed, unsupported, and
budget-exhausted P&R outcomes are valid results, while owner policy decides
whether they are accepted. No execution envelope or policy is copied into the
stable result.

Serialization is reversible and rejects unknown fields, enum values, duplicate
fields, missing fields, and malformed nested structures. Any closure or
verification conclusion is a separate owner/experimental evidence contract, not
a reconstruction from stable result metric names.

A result can then cross the independent
`compile_materialization_plan(job, result, target)` seam. Its immutable,
database-neutral plan contains exact instance and Routing Blockage placements,
Route Segments, Route Vias, and typed owners. Exhausted results retain their
maximum legal geometry as diagnostic plans; unsupported, infeasible, and
independently invalid results carry explicit rejection status. This Module never
writes OA, DEF, GDS, or another persistent database.

Executable plans cross a second, explicit `physical-design.materialize` Action.
Its ToolAdapter seam consumes the canonical job, result, and plan plus a typed
format target.  A successful GDSII implementation emits `layout.gds` and an
immutable Materialization Receipt that binds owner, target, operation, backend
completion, and complete job/result/plan/layout semantic identities; recovery
also compares their complete typed records.
Diagnostic or rejected plans are rejected before backend execution. Unsupported,
backend-unavailable, execution-failed, and invalid-plan/identity outcomes emit a
receipt but never publish a checked layout. The package registers no synthetic
materializer; project assembly must provide a real Adapter, capability, layer
mapping, and master-layout asset. Contract tests use an unregistered GDSII fixture
Adapter and do not establish a product or signoff conclusion.

Physical verification remains a separate owner contract after materialization.
`physical-verification.drc` and `physical-verification.lvs` operations require
the exact `layout.gds` and Materialization Receipt from one producer plus the
owner verification policy; LVS also requires the canonical checked source. Their
artifacts carry receipt, job, result, plan, layout, and source identities,
backend completion, findings, and one of five typed conclusions: clean, violated,
unsupported, backend unavailable, or execution failed. Combined XStream-Calibre
verification and OA-backed verification use the explicit experimental extension
described above, with capability and deck views as owner preflight requirements.
Exit code zero without a parsed authoritative report is execution failure, not
clean or violated evidence.

## Experimental closure and repair

`ClosureCampaignRunner.run(campaign)` is an explicit experimental/owner workflow
above the one-pass deterministic executor. It is reachable only through an
explicit `sigilicon experimental campaign plan/run` request or an owner opt-in;
it is not a stable target operation or a default MCP tool. Every attempt supplies
an already resolved owner/target/operation plan and explicit artifact references.
The campaign validates canonical job, result, standalone closure, Materialization
Plan, Materialization Receipt, managed checked layout/source, DRC, and LVS
identities before making a closure decision. A bound optional output that was not
produced blocks its consumers instead of causing an untyped executor lookup
failure. Execution acceptance records Action execution only; campaign closure
still requires a materialized receipt and every required typed conclusion.

Campaign quality is lexicographic rather than a scalar score. Its dominance order
is: source/layout/result identity; executable materialization; DRC and LVS;
resource overflow, unrouted branches, blockers, group, via, and topology failures;
independent checker and constraint evaluation; PEX and post-layout state; project
qualification; then displacement, area, and power costs. Unknown cost evidence is
never inferred from report metrics. State budget counts distinct typed quality
states, while iteration budget counts owner target/operation attempts.

Feedback is restricted to typed physical owners, fixed blockers, DRC rules, LVS
mismatch categories, or another explicit evidence identity. A campaign does not
perform unattributed global search. PEX, post-layout analysis, and project
qualification cross strict receipt-bound evidence contracts. Their Actions remain
owner-extension seams. A Calibre xRC PEX Adapter, post-layout analysis, and
qualification are available only when an owner explicitly opts into the relevant
experimental extension and supplies a complete attested platform view. Requesting
any unavailable stage yields typed non-conclusion and prevents `closed` rather
than manufacturing success.

Attributed continuation crosses the independent
`compile_closure_repair(...) -> RepairPlan` seam. A project-owned typed policy maps
one exact physical-owner or DRC-rule identity to one exact placement; the compiler
proves owner mobility, manufacturing-grid alignment, legal repair region, hard
constraints, and displacement budget without searching or interpreting report
text. Applying an accepted plan creates a new immutable `PhysicalDesignJob` whose
lineage binds the parent job/result, feedback, source evidence, and RepairPlan.
Unmapped DRC, LVS, PEX, post-layout, and qualification feedback remains explicitly
unsupported. `ClosureCampaignRunner` consumes the public RepairPlan decision and
does not own or duplicate its compilation algorithm. Both the campaign and
RepairPlan are experimental/owner opt-ins; neither changes the stable Job, Result,
Plan, or Receipt schemas.

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

The M0–M5 milestones below describe the opt-in reference implementation and its
owner extensions. They do not promote reference PNR, closure/repair, or
verification into the stable kernel or default MCP inventory.

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

M0 is an experimental contract and correctness reference. It is not an optimizing
placer and is not a default target.

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
- deterministic grid-aligned length compensation and negotiated multi-net skew
  closure against compiled length targets and feasible windows;
- ordered required-region primary paths and signal-derived shield guidance that
  constructs continuous parallel conductors and corresponding cross-layer vias;
- neutral routing benchmark corpus spanning dense gridless multilayer obstacles,
  finite-via orthogonal tracks, transitive multi-net groups, static infeasibility,
  and state/iteration-budget exhaustion;
- deterministic negotiated routing that attributes dynamic blockers, accumulates
  historical congestion cost, and rips up only the affected net or compiled
  Routing Group;
- a compiled Routing Resource Graph that gives gridless corridors, explicit
  tracks, layer segments, and via sites stable identities and capacities while
  presenting A* only a per-net legal-transition and cost Interface;
- capacity-aware convergence that constructs a typed Routing Conflict Set,
  applies a deterministic victim policy, updates present and historical costs,
  and records typed closure or budget termination evidence;
- explicit multi-terminal Routing Trees with stable primary/leaf Route Branch
  ownership, branch-level conflict attribution, safe leaf-only rip-up, and
  deterministic escalation to net or Routing Group scope for shared trunks and
  collective topology, length, via, skew, or shielding policy;
- compiled Physical Owner facts retain stable instance, instance-pin, port, and
  top-level Routing Blockage ownership across placed obstruction, pin-access,
  corridor, and resource
  attribution; fixed and unowned blockers remain explicit;
- typed Routing Placement Pressure prefers exact physical blocker ownership,
  then affected-resource ownership, before a terminal repair scope fallback; a
  deep Placement Repair Problem Module compiles owner scope, local legal
  candidates, predicted resource release, pin-access impact, displacement,
  rejection history, deterministic order, and an independent state budget;
- Placement Repair candidates move only attributed instances or Routing
  Blockages within their explicit local repair regions; acceptance still
  recompiles the Routing Problem and runs negotiated routing plus independent
  evaluation rather than trusting candidate prediction;
- a deterministic Placement-Routing Closure outer loop with displacement,
  routing-improvement, no-repair, repair-state, and repair-iteration evidence;
- typed Routing Closure Quality with explicit lexicographic policy over resource
  overflow, unrouted branches, blocker/group/via/topology failures, independent
  evaluation, physical-owner pressure, routed progress, and displacement;
- every repair records current/candidate quality and an improved, equivalent,
  or regressed decision; fewer conflicts cannot mask increased overflow;
- the experimental Placement-Routing Closure Evidence extension projects final
  conflict and pressure summaries, owner mobility and attribution reason, distinct
  routing/outer-loop termination, Routing Closure Quality, and each evaluated
  attributed repair without exposing mutable routing state or Routing Resource
  Graph internals;
- independent state and iteration budgets, with partial legal routes retained for
  infeasible, unsupported, and exhausted results;
- independent geometry, spacing, resource-coverage, and connectivity checks over
  each returned Routing Solution;
- explicit distinction between infeasibility, capability gaps, and search-budget
  exhaustion;
- partial-stage observability: a successful Placement Solution remains available
  when the requested Routing Solution cannot be completed.

The current experimental reference router deliberately rejects track problems whose terminals
have no legal track access and layer transitions whose Via Definitions lack complete
enclosure or cut-spacing facts.
Those are capability boundaries, not implicit fallbacks. Further routing increments
broaden negotiated group closure and benchmark coverage without weakening the
independent checker or constraint evaluator.

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

- typed DRC/LVS result ingestion with artifact identity and explicit backend
  completion, without weakening signoff authority;
- deterministic experimental closure-campaign provenance connecting each
  target/operation plan and run to its canonical stage artifacts;
- explicit lexicographic Closure Quality and attributed feedback with independent
  state and iteration budgets;
- PEX, post-layout, and qualification have typed artifact contracts and Campaign
  consumers. Calibre xRC PEX is implemented with receipt/source/layout identity,
  fixed three-stage completion proof, and a bounded foundry support view;
  post-layout and qualification still require real owner Adapters and canonical
  specifications before they can move from `unsupported` to satisfied evidence.
  Area and power cost are
  consumed only from specification-bound qualification evidence, never recovered
  from generic report metrics.
