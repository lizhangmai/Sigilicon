---
status: accepted
---

# Use one managed execution lifecycle across design styles

Every operation that executes an EDA tool and produces canonical engineering-run evidence will run as a typed Action through `ProjectRunner.plan(...) -> FlowExecution` and `FlowEngine`. The selected owner and Execution Profile provide the Adapter, while one Flow run exclusively owns capability preflight, workspace-operation binding, logs, artifacts, incidents, evidence, completion, and cleanup. Each invocation has exactly one canonical Sigilicon RunRecord and manifest; backend-native metadata, summaries, and result databases remain ordinary artifacts of that run rather than a second Sigilicon lifecycle. ASIC, analog, mixed-signal, physical-design, and native-OA payloads retain their domain schemas; only their execution and evidence envelope is shared.

OA source/workspace administration remains a separate Module because plan, check, rebuild, and attestation govern a mutable external design database rather than an engineering run. Their reports are workspace/source administration evidence and cannot produce regression, qualification, or signoff conclusions. Native simulation and verification consume the resolved OA workspace through Flow. Cataloged execution has no standalone recorder or second Sigilicon lifecycle; an owner-mandated human entrypoint may remain only as a thin typed-Flow facade.

This rejects both a universal flattened EDA result schema and permanent parallel script/workflow lifecycles. Action declarations may be shared globally, but concrete Adapters are assembled only for the selected owner and profile so unrelated tool families do not become hidden runtime dependencies.

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

Typed plans retain the same source records from which they were resolved.
Layout generation brackets generator execution with exact source reads; native
OA retains netlist/text snapshots and validates parsed contract snapshots; RTL
and AMS plans retain their complete verification/platform records. Constructing
an `ActionPlan` from a later filesystem reread is invalid: the typed value and
its persisted source closure must describe one source state.

## 2026-08-31 custom-layout implementation boundary

Cataloged custom-layout generation and XStream/Calibre verification now enter
through `ProjectRunner.plan(RunRequest.layout(...)) -> FlowExecution`. The layout registry selects
explicit `generate`, `verify-drc`, `verify-lvs`, or `verify-all` Flow targets. Planning
freezes the resolved `LayoutSpec`, generated `LayoutPlan`, owner route, platform
contracts, generator implementation, and canonical netlist sources. Execution
revalidates those exact records before opening the OA workspace and consumes the
already-built plan; it does not import the generator again.

The shared generation and verification backends require a caller-owned
`RunArtifacts` and workspace operation identity. A Flow Adapter therefore writes
completion, native logs, GDS, Calibre reports, and typed evidence beneath the
one parent Flow run and never creates a nested `ArtifactRecord`. Persistent
layout execution has no direct backend wrapper. OA rebuild alone may invoke
layout generation in a disposable workspace because it is an administration
operation with no engineering-run identity. Layout `check` remains direct static
planning and creates no engineering run.

## 2026-08-31 migration completion

The internal standalone layout and Xcelium CLIs and the standalone OA Maestro,
Xcelium, AMS, layout-generation, and layout-verification recorders have been
removed. Their execution cores now accept artifacts owned by the calling Flow.
Design and layout catalogs select typed Flow targets; they do not execute EDA
tools or own run state. Shared source snapshots use one implementation so both
domains bind file bytes, execution bits, and source roots with the same rules.

Composite-IP structural macro linking follows the same rule. The reusable
`asic.structural-link` Action consumes an exact RTL source-set and owner recipe,
validates the immutable dependency release, and owns both Library Compiler and
Design Compiler stages. Its compiled macro DB, DDC, structural report, logs and
uncharacterized evidence share the parent Flow run; an owner Python runner may
not create a parallel structural-link result directory.
