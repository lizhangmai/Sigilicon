---
status: accepted
---

# Use one managed execution lifecycle across design styles

Every operation that executes an EDA tool and produces canonical engineering-run evidence will run as a typed Action through the stable `target.plan` -> `target.run` seam and the deterministic executor. The selected owner target and operation provide the Adapter, while one managed run exclusively owns capability preflight, workspace-operation binding, logs, artifacts, incidents, evidence, completion, and cleanup. Each invocation has exactly one canonical Sigilicon RunRecord and manifest; backend-native metadata, summaries, and result databases remain ordinary artifacts of that run rather than a second Sigilicon lifecycle. ASIC, analog, mixed-signal, physical-design, and native-OA payloads retain their domain schemas; only their execution and evidence envelope is shared.

OA source/workspace administration remains a separate Module because plan, check, rebuild, and attestation govern a mutable external design database rather than an engineering run. Their reports are workspace/source administration evidence and cannot produce regression, qualification, or signoff conclusions. Native simulation and verification consume the resolved OA workspace through the selected target operation. Cataloged execution has no standalone recorder or second Sigilicon lifecycle; an owner-mandated human entrypoint may remain only as a thin typed target facade.

This rejects both a universal flattened EDA result schema and permanent parallel script/workflow lifecycles. The one action registry contains every installed implementation, including implementations marked experimental, but providers remain lazy and a recipe must name an Adapter explicitly. Registration is availability, not fallback, qualification, or an implicit owner opt-in.

Domain planning is also completed before Adapter materialization. Actions that
need a resolved design target, OA assembly, Xcelium cell, AMS platform/release,
or custom-layout plan declare one `plan_input_kind` and receive an `ActionPlan`.
That value carries the typed in-memory plan, its portable persisted projection,
and the exact UTF-8 source snapshots checked by preflight. Adapters are stateless
with respect to repository selection: they may depend on a tool client factory,
but may not capture a `Project`, owner catalog, target, or planning result in
their constructor, and they never re-plan during execution. Each source member
also records a portable scope (`project` or `sigilicon-package`) so editable
package implementation dependencies remain explicit without persisting host
paths.

The `FlowRegistry` owns the complete runtime module for each typed Action:
its contract, its lazy Adapter provider, and, when `plan_input_kind` is
declared, exactly one domain planner. `FlowEngine` asks that registry to plan
only the nodes in the selected target topology. `ProjectRunner` is therefore
only the owner/target composition root; it contains no Action-kind dispatch or
domain planning branches. Built-in design, native-OA/Xcelium, and custom-layout
modules install their own planner/Adapter pairs, while an owner action module uses
the single breaking entrypoint
`register_action_modules(registry, project, owner)`. No compatibility entrypoint
or externally supplied per-node plan map is retained.

Repository assembly names those owner sources only under
`[flow.action_modules]`. The earlier registry-extension vocabulary and the
separate stable/experimental registry builders are deleted. An experimental
implementation is selected by the same explicit owner recipe as any ASIC,
analog, mixed-signal, layout, or native implementation; no owner forwarding
module is required merely to make an installed implementation visible.

Owner workflow composition is identical for every design style. An
`OwnerTarget` contains inputs and explicit `OwnerOperation` values; every
operation names its own recipe and goals. Target-level default recipes are
invalid, because their inheritance made the standard-ASIC catalog behave
differently from analog, mixed-signal, and layout catalogs. `OwnerWorkflow`
is the sole source-bound compiler from catalog + operation + recipe + selected
owner inputs to `FlowSpec`; `ProjectRunner` only binds the resulting plan to
the execution engine.

`ActionPlan` is the sole cross-domain planning envelope and the sole owner of
the exact `SourceMember` closure. Design and layout payloads retain only their
domain invocation state; they do not copy the same closure into a second
wrapper. Native RTL and AMS verification share the same Xcelium invocation
base and execution result, while the AMS payload adds only native-release and
platform-model state. This common envelope does not flatten domain records or
evidence schemas.

Typed plans retain the same source records from which they were resolved.
Layout generation brackets generator execution with exact source reads; native
OA retains netlist/text snapshots and validates parsed contract snapshots; RTL
and AMS plans retain their complete verification/platform records. Constructing
an `ActionPlan` from a later filesystem reread is invalid: the typed value and
its persisted source closure must describe one source state.

## 2026-08-31 custom-layout implementation boundary

Cataloged custom-layout generation and XStream/Calibre verification are an
explicit owner/experimental extension of the stable `target.plan` -> `target.run`
seam. The owner target selects explicit `generate`, `verify-drc`, `verify-lvs`, or
`verify-all` operations. Planning freezes the resolved `LayoutSpec`, generated
`LayoutPlan`, owner route, platform contracts, generator implementation, and
canonical netlist sources. Execution revalidates those exact records before
opening the OA workspace and consumes the already-built plan; it does not import
the generator again.

The shared generation and verification backends require a caller-owned
`RunArtifacts` and workspace operation identity. A target Adapter therefore writes
completion, native logs, GDS, Calibre reports, and typed evidence beneath the
one parent managed run and never creates a nested `ArtifactRecord`. Persistent
layout execution has no direct backend wrapper. OA rebuild alone may invoke
layout generation in a disposable workspace because it is an administration
operation with no engineering-run identity. Layout `check` remains direct static
planning and creates no engineering run.

## 2026-08-31 migration completion

The internal standalone layout and Xcelium CLIs and the standalone OA Maestro,
Xcelium, AMS, layout-generation, and layout-verification recorders have been
removed. Their execution cores now accept artifacts owned by the calling target
operation.
Design and layout catalogs select typed target operations; they do not execute EDA
tools or own run state. Shared source snapshots use one implementation so both
domains bind file bytes, execution bits, and source roots with the same rules.

Composite-IP structural macro linking follows the same rule. The reusable
`asic.structural-link` Action consumes an exact RTL source-set and owner recipe,
validates the immutable dependency release, and owns both Library Compiler and
Design Compiler stages. Its compiled macro DB, DDC, structural report, logs and
uncharacterized evidence share the parent managed run; an owner Python runner may
not create a parallel structural-link result directory.

## 2026-08-31 final boundary

The public lifecycle is `target.plan` -> `target.run` -> `run.inspect`/`run.cancel`.
Each request names `owner`, `target`, and `operation`; execution accepts only the
immutable plan identity and produces one run identity. The old profile and split
design/layout selection surfaces are deleted; they do not form a second API and
have no compatibility interpretation. Reference PNR, closure/repair, OA-XStream, and
combined XStream-Calibre remain explicit `sigilicon.experimental`/owner opt-ins;
bounded campaigns use only the explicit `sigilicon experimental campaign plan/run`
CLI and are absent from default MCP.

Project-owned Python modules are importable only inside one shared bounded import
context tied to the selected project root. Layout generators, native diagnostic
processors, and composite-IP architecture validators use that same boundary, so
console-script and `python -m` launches have identical project-module semantics.
The package root no longer lazily re-exports project workflow classes and CLI
composition calls `Project.from_file` directly; the explicit public Python seam
is `sigilicon.project` plus the domain/workflow modules it names.
