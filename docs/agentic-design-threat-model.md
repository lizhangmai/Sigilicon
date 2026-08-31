# Agentic circuit-design threat model

Status: stable/experimental boundary finalized, 2026-08-31.

This model covers Sigilicon's future native MCP process, its application Interface,
managed artifacts, and deterministic target/operation EDA adapters. MCP clients, models,
repository text, raw logs, reports, artifact metadata, and external tools are
untrusted inputs. Git-owned contracts become design facts only after existing
owner/catalog validation; EDA conclusions become evidence only after authorized
execution, authoritative parsing, and identity checks.

The official MCP 2026-07-28 specification requires URI and tool-input validation,
access controls, output sanitization, rate limits, and user control. It also states
that state handles are names rather than capabilities and must be authorized on
every request. Sigilicon therefore never relies on connection-local state or an
unguessable run identifier as authorization.

## Assets and trust boundaries

Protected assets are canonical Git source, owner and platform contracts, PDK and
license secrets, OA databases, execution credentials, managed workspaces, artifact
identity/provenance, authoritative reports, and qualification decisions.

Trust boundaries are:

1. model/client request to MCP schema and authorization;
2. MCP handler to the shared Sigilicon application Interface;
3. application Interface to an owner-selected target/operation Adapter capability;
4. adapter to external process or OA bridge;
5. external output to fail-closed parser and immutable artifact;
6. Candidate artifact to human-approved Promotion Plan and ordinary Git workflow.

## Required controls

| Threat | Required control | Fail-closed test |
| --- | --- | --- |
| path traversal, absolute path, or symlink escape | semantic IDs only at MCP; explicit ProjectContext; owner-root resolution; existing no-follow/dirfd artifact I/O | `..`, absolute, backslash, symlink, and replaced-inode cases are rejected |
| generic command or environment injection | no shell/command/env fields; allowlisted owner target/operation and Adapter; guarded process Module owns argv | shell metacharacters and option-like identities never reach an executor |
| cross-owner artifact substitution | owner+kind reference; release identity required across owners; typed artifact and authorization checked again on read | mismatched owner, kind, typed value, or missing release identity is rejected |
| Candidate identity drift | every parent/reference resolves to an immutable typed value and its declared owner/kind/lineage | altered structure or substituted artifact invalidates the Candidate |
| fake/offline false success | producer class and backend completion are typed; offline/fake cannot issue qualification/signoff pass | zero exit, existing file, or fake parser cannot create passing evidence |
| missing/malformed authoritative report | completion requires executed backend, parsed report, checked identity, and zero exit | missing or malformed report yields execution failure/non-conclusion |
| prompt injection in logs, reports, or repository text | treat text as data; expose typed facts and bounded escaped excerpts; never turn content into tool arguments or authorization | malicious instructions do not change next operations or permissions |
| secret, PDK path, or oversized output leakage | response projection allowlist; path redaction; size/page limits; private cache scope | secrets and site paths are absent; oversized results return resource links |
| replay or duplicate request | idempotency identity binds caller, owner/target/operation, input, policy, and execution-environment identity; immutable terminal records | duplicate call returns the same handle/result or a typed conflict; a changed environment cannot reuse an old target/operation attempt |
| forged semantic repair | proposal cannot carry a pass conclusion; owner policy recompiles a typed Repair Plan and the campaign verifies parent/child lineage | wrong campaign, attribution, evidence, owner, repair kind, or child parent is rejected before continuation |
| run-handle guessing or confused deputy | opaque handle plus authorization on every request; bounded lifetime and owner binding | another principal/owner cannot inspect, cancel, or resume a run |
| cancellation race | cooperative managed process-group cancellation and immutable terminal state | cancel cannot rewrite completed state; cancelled outputs cannot appear valid |
| concurrent OA/workspace mutation | existing operation lock, exact lease, open-view/lock checks, confirmation, mutation scope, receipt | second writer, live view, replaced lock, or expired lease blocks mutation |
| source promotion without approval | MCP exposes Promotion Plan only; no source mutation tool; ordinary Git review remains authoritative | Candidate data cannot select a repository write path or apply a patch |
| protocol capability confusion | per-request negotiated MCP capabilities and explicit Sigilicon capability checks | an unadvertised extension or ungranted role cannot widen tool/resource access |

## Stable MCP boundary

The default MCP server exposes only `project.inspect`, `target.plan`, `target.run`,
`run.inspect`, `run.cancel`, `candidate.validate`, and
`candidate.promotion_plan`. Every target operation is selected by the explicit
`owner`, `target`, and `operation`; execution requires the immutable
`plan_identity`. There is no compatibility alias for an earlier selection
surface.

Bounded campaigns are outside the default MCP boundary. They require an explicit
experimental CLI request:

```text
sigilicon experimental campaign plan ...
sigilicon experimental campaign run ...
```

OA-XStream and combined XStream-Calibre are explicit owner opt-ins. They must not be reached through a
default target catalog or silently selected by an MCP handler.

Stable resource identity follows the real ownership hierarchy:
`sigilicon://owners/{owner}/targets/{target}/operations/{operation}/runs/{run}/manifest`.
The owner, target, operation, and run components are all authorized on every
request; a run handle is an identifier, not a capability.

## Audit and retention

Audit records contain the authenticated principal, requested capability, semantic
owner/target, input and policy identities, adapter/tool identity, run handle,
timestamps, terminal state, artifact links, and confirmation identity. They do not
record secrets, unrestricted environment data, complete PDK paths, or raw prompt
content. Resource and handle retention is explicit and bounded; terminal evidence
remains immutable for the project retention period.

Experimental Design Campaign checkpoints are canonical states referenced by append-only,
monotonic sequence events. A mutable state pointer is only a cache and is rebuilt from
the ordered events after a missing or partial write. Baseline and continuation execution
failures are recorded as terminal fail-closed states, including when no Candidate
iteration could be observed.

OA mutation and real EDA require the project launcher/session conditions, explicit
grant capability, and the existing OA/target safety tests; MCP cannot weaken those
controls or reinterpret regression evidence as qualification.
