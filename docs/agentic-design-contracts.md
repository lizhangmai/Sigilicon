# Agentic circuit-design contract baseline

Status: single-attempt/campaign boundary finalized, 2026-08-31.

This document fixes the reusable Sigilicon vocabulary and integration inventory for
agent-assisted circuit design. It does not define an IP topology, PDK fact, project
layout, qualification threshold, or site execution environment. Project assembly
continues to enter through `sigilicon.toml` and its selected owner catalogs; there is
no agent-specific project inventory.

The architecture follows a clean-room boundary. Public PANDA papers may inform the
method of structured stage artifacts, separation of semantic planning from
deterministic execution, and post-layout feedback. Sigilicon does not consume or
reproduce PANDA source, prompts, request/result JSON, `analog.*` handlers, binaries,
MCP-style conventions, or environment variables.

## Frozen language

- **Design Brief** records user intent, application context, preferences, confirmed
  requirements, and explicitly provisional assumptions. It is not a qualification
  specification.
- **Canonical Qualification Spec** is the project-owner source that alone defines
  pass/fail requirements, coverage, and evidence role.
- **Design Candidate** is an immutable manifest of owner/kind-bound stage artifact
  references. Candidate identity is a semantic locator projected from typed domain
  names and lineage; correctness comes from resolving and comparing the typed values.
- **Design Evidence** is an immutable observation bound to subject, source, spec,
  producer, and tool identities. Evidence records what ran and what was parsed; it
  does not contain a policy decision.
- **Design Decision** is an owner-policy evaluation of identity-matched Candidate
  and Design Evidence. It cannot repair missing evidence.
- **Design Campaign** is a bounded, durable multi-round workflow
  above FlowEngine. Every attempt is one resolved owner/target/operation plan. A
  violated attempt stops in `proposal_required`; only an explicit client proposal
  can let owner policy compile a Repair Plan and derive the child Candidate and
  next attempt. It is available only through the independent
  `sigilicon campaign plan/run` CLI and is not a default MCP tool.
- **Promotion Plan** is a non-mutating description of semantic source roles,
  required regression, evidence bundle, and unresolved risk. It contains no write
  path or patch. Applying it is outside the MCP interface and always requires human
  approval.

These terms are expressed in source, immutable types, tests, and ADRs. Sigilicon's
`CONTEXT.md` is intentionally unchanged.

## Schema invariants

Stage artifacts keep separate schemas; there is no universal `design.json`. The
first front-end schema family contains:

- `design.brief.v1`;
- `circuit.topology-proposal.v1`;
- `circuit.sizing-problem.v1`;
- `circuit.sizing-result.v1`;
- `design.candidate.v1`;
- `design.evidence.v1`;
- `design.decision.v1`.

Every artifact has an exact schema version, kind, and owner. Canonical JSON uses
UTF-8, sorted object keys, deterministic indentation, finite values, a trailing
newline. Decoding rejects unknown, duplicate,
missing, malformed, non-canonical, or unknown-enum fields. Artifact references
bind kind, owner, and semantic identity. A reference to another owner also requires an
explicit release identity; unpublished source-level composition stays within its
owner component graph and cannot be laundered through an artifact reference.

Topology creation has one explicit ownership seam. An owner-side
`SourceAuthoredTopologyAdapter` normalizes its validated `DesignSpec` and canonical
netlist into the typed topology. The `circuit-design.source` execution Action uses the
existing `source-assets` Adapter only to select and ingest that already-authored
typed artifact; it is not a second topology generator or a parallel inventory.
The resolved target/operation plan carries every selected UTF-8 member record and its executable
bit together with the exact owner-scoped Git commit and change records. Preflight
and materialization compare those typed values again, so a dirty file changing
without changing its path cannot reuse an earlier authorization.

No artifact serializes an absolute path, environment mapping, raw command, or EDA
invocation. Source adapters accept an explicit `ProjectContext` or an already
validated owner source value. Candidate outputs stay under project-managed ignored
artifact storage. Source promotion is not part of this schema family.

Evidence role (`diagnostic`, `regression`, `qualification`, or `signoff`), DUT
level (`l0` through `l4`), and coverage scope remain independent. Offline and fake
producers can record non-conclusions and violations for contract testing, but can
never produce a passing qualification or signoff decision. A normal exit, an
output file, a model statement, or an unparsed report is not passing evidence.

## Native MCP inventory

The protocol target is the official MCP 2026-07-28 specification. Resources and
tools are projections of existing Sigilicon Interfaces, not owners of domain logic.
MCP Tasks are an optional negotiated extension; Sigilicon durable run identity is
the base application contract even when the extension is unavailable.

Read-only resource inventory (Phase 2 exposes the minimal implemented subset; later
rows remain reserved inventory, not alternate project indexes):

| Resource template | Projection | Capability | State |
| --- | --- | --- | --- |
| `sigilicon://project/{project_id}` | validated ProjectContext and catalog links | `read-project` | Phase 2 |
| `sigilicon://owners/{owner}/catalog` | owner-selected targets and contracts | `read-project` | Phase 2 |
| `sigilicon://owners/{owner}/targets/{target}/operations/{operation}/runs/{run_id}/manifest` | owner/target/operation-bound durable run result | `read-project` | Phase 2 |
| `sigilicon://owners/{owner}/targets/{target}` | one owner target projection | `read-project` | reserved |
| `sigilicon://contracts/{identity}` | bounded typed contract summary and source identity | `read-project` | reserved |
| `sigilicon://schemas/{artifact_kind}` | the Sigilicon-owned schema | `read-project` | reserved |
| `sigilicon://artifacts/{identity}` | one authorized immutable artifact | `read-project` | reserved |
| `sigilicon://evidence/{identity}/summary` | bounded parsed evidence, never raw authority | `read-project` | reserved |

Tool baseline:

| Tool | First phase | Capability | Input rule |
| --- | --- | --- | --- |
| `project.inspect` | Phase 2 | `read-project` | explicit server ProjectContext and semantic owner selector |
| `target.plan` | Phase 2 | `plan-target` | cataloged owner, target, operation, and bounded options |
| `target.run` | Phase 3 | `execute-derived` | immutable `plan_identity` plus confirmed budget; the approved record owns owner/target/operation |
| `run.inspect` | Phase 2 | `read-project` | owner/target/operation plus validated run identity |
| `run.cancel` | Phase 3 | `execute-derived` | owner/target/operation and opaque run identity; cooperative managed cancellation |
| `candidate.validate` | Phase 2 | `plan-target` | exact canonical Candidate/stage JSON plus cataloged owner; no paths |
| `candidate.promotion_plan` | Phase 5 | `plan-target` | validated Candidate and evidence bundle; never writes source |

The stable MCP inventory is exactly the project/target/run/candidate set above;
campaign planning and execution are not MCP tools. A bounded campaign is available
only after an explicit request through the independent CLI:

```text
sigilicon campaign plan ...
sigilicon campaign run ...
```

The campaign CLI requires owner policy, explicit target/operation scope,
budgets, and stop conditions. It does not create a compatibility alias for any
removed pre-target API.

There is no generic shell, arbitrary file reader, arbitrary absolute path,
environment editor, raw EDA command, OA mutation shortcut, PANDA compatibility
tool, or `apply_promotion` tool. Tool lists are deterministic; a future
authenticated transport must additionally filter them by per-request
authorization. Every tool has strict input and output schemas, structured results,
a short bounded summary, semantic resource links, explicit
conclusion/non-conclusion, and allowed next operations.

Capability levels are cumulative only when explicitly granted:

1. `read-project` reads authorized Git contracts and existing artifacts;
2. `plan-target` performs pure validation and deterministic planning;
3. `execute-derived` may invoke approved adapters and write ignored artifacts;
4. `mutate-workspace` additionally requires the existing exact OA lease,
   confirmation, mutation scope, and receipt checks;
5. `promote-source` is not exposed by the first MCP server.

### Phase 2 binding

Source-dependent reads and historical Run reads are separate application seams.
`AgenticReadInterface` requires one explicit, fully loaded `Project` and owns
`project.inspect`, `target.plan`, and owner-bound `candidate.validate`.
`RunReadInterface` requires only the explicit `ProjectContext`; it owns
`run.inspect` and must not load current owner catalogs or recipes. The read CLI and
native MCP composition bind the narrowest seam for each operation. The Candidate
operation and Candidate CLI both delegate to `AgenticReadInterface`, which applies
the same typed `validate_design_candidate` domain validation. MCP accepts semantic
identities or bounded canonical JSON only. The read-only Phase 2 tools have no path, shell,
environment, executor, cancellation, OA, or promotion input.

The local entry point is `sigilicon-mcp --project-root <root>`. Project selection is
launcher configuration rather than a model-call argument, and cwd discovery is not
used. The official Python MCP SDK is an optional `sigilicon[mcp]` dependency, so
top-level package import and the ordinary CLI remain usable without it. Phase 2
implements stdio only; authenticated Streamable HTTP and principal authorization
remain deployment work and must not be inferred from the local server.

The first Skills are `inspect-design-status` and `plan-circuit-candidate`. They own
workflow order, stop conditions, missing-result handling, and reporting shape. They
contain no IP topology, PDK fact, product threshold, or EDA command.

## Pilot and acceptance baseline

Pilot order is fixed as INV/TG physical parity, CDAC_BOTTOM_SWITCH sizing and
pre-layout diagnostic normalization (SAR_ASYNC_CLOCK_GATE may follow), then
CALIBRATED_DYNAMIC_COMPARATOR full-loop evidence, and only later the complete MX
Block. The existing CDAC sizing campaign remains an owner-scoped diagnostic
and its algorithm, candidate set, measurements, and thresholds are not changed by
normalization; it is not a default MCP campaign operation.

The `DesignCampaignRunner` owns the cross-round state machine;
`FlowEngine` remains a single-round typed DAG executor. A campaign source contains exactly one baseline
attempt plus an optional continuation template and Repair Policy, never a
pre-enumerated second result. Durable start/resume checkpoints are append-only,
sequence-checked, recoverable across processes, and bind the grant and execution
environment identity. Invalid evidence, invalid lineage, backend failure, or an
exhausted iteration/state/node/time budget reaches an explicit fail-closed terminal
state. Sigilicon never invokes an LLM: client proposals are untrusted semantic input
and cannot assert verification success.

`candidate.promotion_plan` shares the read application Interface across Python,
CLI, and MCP. It accepts only a validated Candidate, exact evidence-bound passing
Decision, owner requirements, and semantic Candidate stage roles. Its immutable
result always declares `human_approval_required = true` and
`writes_canonical_source = false`; no `apply_promotion` operation exists.

Phase acceptance requires strict round-trip and rejection tests, explicit
identity/owner lineage, fake/offline non-conclusion, path and injection defenses,
one shared application Interface for Python/CLI/MCP, package tests, wheel build and
clean-wheel import/CLI smoke, project `check-designs`, and `git diff --check` in
both repositories. Real Adapter evidence may be consumed only through the existing
OA/EDA safety and receipt-bound parsers. Such evidence retains its declared role and
DUT level; a physical regression pilot cannot be upgraded into product
qualification.
