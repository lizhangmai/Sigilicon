"""Deterministic planning and managed local execution for typed design Flows."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
import uuid

from sigilicon.artifacts import atomic_write_json, read_json_object
from sigilicon.flow.environment import (
    capability_available,
    stale_platform_asset_members,
)
from sigilicon.flow.model import (
    ActionArtifact,
    ActionContext,
    AdapterExecution,
    CollectedActionResult,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowContractError,
    FlowExecutionError,
    FlowPlan,
    FlowResult,
    FlowSpec,
    InputArtifact,
    NodeOutcome,
    PolicySpec,
    PlannedNode,
    PreflightCheck,
    PreflightResult,
    ProducedArtifact,
    identifier,
    owner_identity,
    run_identity,
)
from sigilicon.flow.policy import EvaluatedPolicy, evaluate_policy
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.serialization import canonical_digest, json_value
from sigilicon.flow.source_revision import (
    resolve_node_source_revision,
    source_revision_payload,
    stale_source_members,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _plan_payload(
    spec: FlowSpec,
    profile: ExecutionProfile,
    target_id: str,
    planned: tuple[PlannedNode, ...],
    topology: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "contract_kind": "resolved-flow-plan",
        "owner": spec.owner,
        "flow": spec.flow_id,
        "execution_profile": {
            "owner": profile.owner,
            "name": profile.profile_id,
        },
        "target": target_id,
        "topology": list(topology),
        "nodes": [
            {
                "id": item.node.node_id,
                "action": item.node.action_kind,
                "adapter": item.adapter,
                "action_config": json_value(item.node.config),
                "adapter_config": json_value(item.adapter_config),
                "required_capabilities": list(item.required_capabilities),
                "platform_assets": [
                    json_value(requirement) for requirement in item.platform_assets
                ],
                "policy": item.node.policy,
                "dependencies": list(item.dependencies),
                "bindings": [json_value(binding) for binding in item.node.bindings],
                "source_revision": (
                    None
                    if item.source_revision is None
                    else source_revision_payload(item.source_revision)
                ),
            }
            for item in planned
        ],
        "policies": [json_value(policy) for policy in spec.policies],
    }


class FlowEngine:
    """Deep module that plans typed graphs and executes them through Adapters."""

    def __init__(self, registry: FlowRegistry) -> None:
        self._registry = registry

    def plan(
        self,
        spec: FlowSpec,
        target_id: str,
        profile: ExecutionProfile,
    ) -> FlowPlan:
        if profile.owner != spec.owner:
            raise FlowContractError(
                f"Execution Profile owner {profile.owner!r} does not match "
                f"Flow owner {spec.owner!r}"
            )
        target = spec.target(target_id)
        node_order = {node.node_id: index for index, node in enumerate(spec.nodes)}
        dependencies: dict[str, tuple[str, ...]] = {}
        source_revisions: dict[str, Any] = {}
        for node in spec.nodes:
            contract = self._registry.action(node.action_kind)
            selection = profile.selection(node.action_kind)
            if selection.adapter not in contract.adapters:
                raise FlowContractError(
                    f"Adapter {selection.adapter!r} cannot implement "
                    f"Action {contract.kind!r}"
                )
            source_revisions[node.node_id] = resolve_node_source_revision(
                spec,
                node,
                contract,
            )
            if node.policy is not None:
                spec.policy(node.policy)
            binding_inputs: dict[str, list[Any]] = {}
            data_dependencies: list[str] = []
            for binding in node.bindings:
                consumer = contract.input(binding.input)
                producer_node = spec.node(binding.producer)
                producer_contract = self._registry.action(producer_node.action_kind)
                producer = producer_contract.output(binding.output)
                if not consumer.accepts(producer.kind):
                    raise FlowContractError(
                        f"binding {binding.producer}.{binding.output} kind "
                        f"{producer.kind!r} is incompatible with {node.node_id}."
                        f"{binding.input} kind {consumer.kind!r}"
                    )
                binding_inputs.setdefault(binding.input, []).append(binding)
                data_dependencies.append(binding.producer)
            for port in contract.inputs:
                count = len(binding_inputs.get(port.role, ()))
                if port.required and count == 0:
                    raise FlowContractError(
                        f"node {node.node_id!r} is missing required input {port.role!r}"
                    )
                if not port.multiple and count > 1:
                    raise FlowContractError(
                        f"node {node.node_id!r} input {port.role!r} has multiple bindings"
                    )
            for predecessor in node.order_after:
                spec.node(predecessor)
            dependencies[node.node_id] = tuple(
                dict.fromkeys((*data_dependencies, *node.order_after))
            )

        selected: set[str] = set()
        pending = list(target.goals)
        while pending:
            node_id = pending.pop()
            spec.node(node_id)
            if node_id in selected:
                continue
            selected.add(node_id)
            pending.extend(dependencies[node_id])

        remaining = {
            node_id: {item for item in dependencies[node_id] if item in selected}
            for node_id in selected
        }
        ready = sorted(
            (node_id for node_id, values in remaining.items() if not values),
            key=node_order.__getitem__,
        )
        topology: list[str] = []
        while ready:
            node_id = ready.pop(0)
            topology.append(node_id)
            for candidate in sorted(selected - set(topology), key=node_order.__getitem__):
                if node_id in remaining[candidate]:
                    remaining[candidate].remove(node_id)
                    if not remaining[candidate] and candidate not in ready:
                        ready.append(candidate)
            ready.sort(key=node_order.__getitem__)
        if len(topology) != len(selected):
            cyclic = sorted(selected - set(topology), key=node_order.__getitem__)
            raise FlowContractError(f"Flow contains a dependency cycle: {cyclic}")
        planned_items: list[PlannedNode] = []
        for node_id in topology:
            node = spec.node(node_id)
            contract = self._registry.action(node.action_kind)
            selection = profile.selection(node.action_kind)
            planned_items.append(
                PlannedNode(
                    node=node,
                    adapter=selection.adapter,
                    adapter_config=selection.config,
                    required_capabilities=tuple(
                        dict.fromkeys(
                            (
                                *contract.required_capabilities,
                                *selection.required_capabilities,
                            )
                        )
                    ),
                    platform_assets=contract.platform_assets,
                    dependencies=dependencies[node_id],
                    source_revision=source_revisions[node_id],
                )
            )
        planned = tuple(planned_items)
        topology_tuple = tuple(topology)
        payload = _plan_payload(spec, profile, target_id, planned, topology_tuple)
        return FlowPlan(
            spec=spec,
            profile=profile,
            target=target,
            nodes=planned,
            topology=topology_tuple,
            fingerprint=canonical_digest(payload),
        )

    def plan_record(self, plan: FlowPlan) -> dict[str, Any]:
        """Return the canonical, source-only record for a resolved plan."""

        payload = _plan_payload(
            plan.spec,
            plan.profile,
            plan.target.target_id,
            plan.nodes,
            plan.topology,
        )
        payload["fingerprint"] = plan.fingerprint
        return payload

    def preflight(
        self,
        plan: FlowPlan,
        environment: ExecutionEnvironment,
    ) -> PreflightResult:
        """Purely compare planned semantic requirements with current site facts."""

        checks: list[PreflightCheck] = []
        seen_adapters: set[str] = set()
        seen_capabilities: set[str] = set()
        seen_assets: set[tuple[str, str, tuple[str, ...]]] = set()
        for planned in plan.nodes:
            if planned.adapter not in seen_adapters:
                seen_adapters.add(planned.adapter)
                available = self._registry.has_adapter(planned.adapter)
                checks.append(
                    PreflightCheck(
                        requirement=planned.adapter,
                        requirement_kind="adapter",
                        status="available" if available else "missing",
                        identity=(
                            self._registry.adapter(planned.adapter).version
                            if available
                            else None
                        ),
                    )
                )
            if planned.source_revision is not None:
                stale = stale_source_members(planned.source_revision)
                checks.append(
                    PreflightCheck(
                        requirement=planned.source_revision.revision_id,
                        requirement_kind="source-revision",
                        status="available" if not stale else "stale",
                        expected=planned.source_revision.fingerprint,
                        identity=planned.source_revision.revision_id,
                        digest=planned.source_revision.fingerprint,
                    )
                )
            for capability in planned.required_capabilities:
                if capability in seen_capabilities:
                    continue
                seen_capabilities.add(capability)
                resolved_capability = environment.capabilities.get(capability)
                status = (
                    "missing"
                    if resolved_capability is None
                    else "available"
                    if capability_available(resolved_capability)
                    else "unavailable"
                )
                checks.append(
                    PreflightCheck(
                        requirement=capability,
                        requirement_kind="capability",
                        status=status,
                        identity=(
                            None
                            if resolved_capability is None
                            else resolved_capability.identity
                        ),
                    )
                )
            for requirement in planned.platform_assets:
                requirement_key = (
                    requirement.role,
                    requirement.kind,
                    requirement.members,
                )
                if requirement_key in seen_assets:
                    continue
                seen_assets.add(requirement_key)
                asset = environment.platform_asset(requirement.role)
                missing_members = (
                    set(requirement.members)
                    - {member.role for member in asset.members}
                    if asset is not None
                    else set()
                )
                status = (
                    "missing"
                    if asset is None
                    else "incompatible"
                    if asset.kind != requirement.kind
                    else "incomplete"
                    if missing_members
                    else "stale"
                    if stale_platform_asset_members(asset)
                    else "available"
                )
                checks.append(
                    PreflightCheck(
                        requirement=requirement.role,
                        requirement_kind="platform-asset",
                        status=status,
                        expected=requirement.kind,
                        identity=None if asset is None else asset.identity,
                        digest=None if asset is None else asset.digest,
                    )
                )
        status = (
            "ready"
            if all(check.status == "available" for check in checks)
            else "blocked"
        )
        fingerprint = canonical_digest(
            {
                "schema": 1,
                "plan_fingerprint": plan.fingerprint,
                "checks": [json_value(check) for check in checks],
            }
        )
        return PreflightResult(status, tuple(checks), fingerprint)

    def preflight_record(
        self,
        plan: FlowPlan,
        result: PreflightResult,
    ) -> dict[str, Any]:
        return {
            "schema": 1,
            "contract_kind": "flow-preflight",
            "owner": plan.spec.owner,
            "flow": plan.spec.flow_id,
            "target": plan.target.target_id,
            "execution_profile": plan.profile.profile_id,
            "status": result.status,
            "checks": [json_value(check) for check in result.checks],
            "plan_fingerprint": plan.fingerprint,
            "fingerprint": result.fingerprint,
        }

    def run(
        self,
        plan: FlowPlan,
        *,
        artifact_root: Path,
        environment: ExecutionEnvironment | None = None,
        run_id: str | None = None,
        resume: bool = False,
    ) -> FlowResult:
        current_environment = environment or ExecutionEnvironment()
        preflight = self.preflight(plan, current_environment)
        if preflight.status != "ready":
            missing = [
                check.requirement
                for check in preflight.checks
                if check.status != "available"
            ]
            raise FlowExecutionError(
                f"Flow preflight is blocked by requirements: {missing}"
            )
        identity = run_identity(run_id or uuid.uuid4().hex)
        store_root = Path(artifact_root).resolve()
        if store_root == Path(store_root.anchor):
            raise FlowExecutionError("artifact root cannot be a filesystem root")
        run_root = (
            store_root
            / "flows"
            / plan.spec.owner
            / plan.spec.flow_id
            / "runs"
            / identity
        )
        if not run_root.resolve(strict=False).is_relative_to(store_root):
            raise FlowExecutionError("Flow Run path escaped the artifact root")
        if run_root.exists() and not resume:
            raise FlowExecutionError(f"Flow Run already exists: {identity}")
        if resume and not run_root.is_dir():
            raise FlowExecutionError(f"cannot resume missing Flow Run: {identity}")
        run_root.mkdir(parents=True, exist_ok=resume)

        atomic_write_json(run_root / "resolved_plan.json", self.plan_record(plan))
        atomic_write_json(
            run_root / "preflight.json",
            self.preflight_record(plan, preflight),
        )

        outcomes: dict[str, NodeOutcome] = {}
        interrupted = False
        for planned in plan.nodes:
            node = planned.node
            if interrupted:
                outcomes[node.node_id] = NodeOutcome(
                    node_id=node.node_id,
                    status="blocked",
                    execution_status=None,
                    result_status=None,
                    policy_status=None,
                    artifacts=MappingProxyType({}),
                    facts=MappingProxyType({}),
                    reason="Flow execution was interrupted",
                )
                continue
            block_reason = self._block_reason(node, outcomes)
            if block_reason is not None:
                outcomes[node.node_id] = NodeOutcome(
                    node_id=node.node_id,
                    status="blocked",
                    execution_status=None,
                    result_status=None,
                    policy_status=None,
                    artifacts=MappingProxyType({}),
                    facts=MappingProxyType({}),
                    reason=block_reason,
                )
                continue
            inputs = self._materialize_inputs(node, outcomes)
            action = self._registry.action(node.action_kind)
            adapter = self._registry.adapter(planned.adapter)
            environment_payload = self._environment_payload(
                planned,
                current_environment,
            )
            interface_fingerprint = canonical_digest(action)
            execution_fingerprint = canonical_digest(
                {
                    "schema": 1,
                    "action": node.action_kind,
                    "adapter": planned.adapter,
                    "adapter_version": adapter.version,
                    "action_config": json_value(node.config),
                    "adapter_config": json_value(planned.adapter_config),
                    "execution_environment": environment_payload,
                    "inputs": {
                        role: {
                            "kind": artifact.kind,
                            "digest": artifact.digest,
                            "producer": artifact.producer,
                            "qualifiers": json_value(artifact.qualifiers),
                        }
                        for role, artifact in sorted(inputs.items())
                    },
                    "source_revision": (
                        None
                        if planned.source_revision is None
                        else source_revision_payload(planned.source_revision)
                    ),
                    "interface_fingerprint": interface_fingerprint,
                }
            )
            node_root = run_root / "nodes" / node.node_id
            reused = self._reusable_outcome(
                node_root,
                run_root=run_root,
                node_id=node.node_id,
                interface_fingerprint=interface_fingerprint,
                execution_fingerprint=execution_fingerprint,
            )
            if reused is not None:
                policy = (
                    None if node.policy is None else plan.spec.policy(node.policy)
                )
                evaluation = evaluate_policy(policy, reused.facts)
                self._write_policy_receipt(node_root, evaluation, policy)
                outcomes[node.node_id] = NodeOutcome(
                    node_id=node.node_id,
                    status=evaluation.status,
                    execution_status="succeeded",
                    result_status="valid",
                    policy_status=evaluation.status,
                    artifacts=reused.artifacts,
                    facts=reused.facts,
                    reused=True,
                    execution_fingerprint=execution_fingerprint,
                )
                continue
            if node_root.exists():
                self._remove_managed_node(node_root, run_root)
            work_root = node_root / "work"
            output_root = node_root / "outputs"
            work_root.mkdir(parents=True)
            output_root.mkdir()
            context = ActionContext(
                node_id=node.node_id,
                action=action,
                run_root=run_root,
                node_root=node_root,
                work_root=work_root,
                output_root=output_root,
                inputs=MappingProxyType(inputs),
                action_config=node.config,
                adapter_config=planned.adapter_config,
                capabilities=MappingProxyType(
                    {
                        capability: current_environment.capabilities[capability]
                        for capability in planned.required_capabilities
                    }
                ),
                platform_assets=MappingProxyType(
                    self._resolved_platform_assets(planned, current_environment)
                ),
                source_revision=planned.source_revision,
            )
            request = {
                "schema": 1,
                "contract_kind": "action-request",
                "node": node.node_id,
                "action": node.action_kind,
                "adapter": planned.adapter,
                "adapter_version": adapter.version,
                "action_config": json_value(node.config),
                "adapter_config": json_value(planned.adapter_config),
                "execution_environment": environment_payload,
                "inputs": {
                    role: self._artifact_payload(artifact, run_root)
                    for role, artifact in sorted(inputs.items())
                },
                "source_revision": (
                    None
                    if planned.source_revision is None
                    else source_revision_payload(planned.source_revision)
                ),
                "interface_fingerprint": interface_fingerprint,
                "execution_fingerprint": execution_fingerprint,
            }
            atomic_write_json(node_root / "action_request.json", request)
            started = _utc_now()
            execution = AdapterExecution("failed")
            collected = CollectedActionResult(status="failed")
            artifacts: dict[str, ActionArtifact] = {}
            facts: dict[str, Any] = {}
            result_status = "failed"
            error: str | None = None
            try:
                diagnostics = adapter.validate_inputs(context)
                if diagnostics:
                    raise FlowExecutionError("; ".join(diagnostics))
                adapter.prepare(context)
                execution = adapter.execute(context)
            except KeyboardInterrupt:
                interrupted = True
                execution = AdapterExecution("cancelled")
                error = "interrupted"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                try:
                    canonical_digest(execution.details)
                except Exception as exc:
                    execution = AdapterExecution(
                        execution.status,
                        execution.exit_code,
                    )
                    error = f"invalid execution details: {type(exc).__name__}: {exc}"
                if error is None and execution.status != "succeeded":
                    interrupted = execution.status == "cancelled"
                    error = f"Adapter execution ended with {execution.status}"
                elif error is None:
                    try:
                        collected = adapter.collect_result(context, execution)
                        artifacts = self._validate_collected(
                            context,
                            collected,
                            run_root=run_root,
                        )
                        facts = dict(collected.facts)
                        missing_facts = set(action.facts) - set(facts)
                        unknown_facts = set(facts) - set(action.facts)
                        if collected.status == "valid" and missing_facts:
                            raise FlowExecutionError(
                                f"Action {node.node_id!r} omitted Facts "
                                f"{sorted(missing_facts)}"
                            )
                        if unknown_facts:
                            raise FlowExecutionError(
                                f"Action {node.node_id!r} emitted undeclared Facts "
                                f"{sorted(unknown_facts)}"
                            )
                        canonical_digest(collected.details)
                        self._content_fingerprint(artifacts, facts)
                        result_status = collected.status
                    except KeyboardInterrupt:
                        interrupted = True
                        execution = AdapterExecution("cancelled")
                        collected = CollectedActionResult(status="failed")
                        artifacts = {}
                        facts = {}
                        error = "interrupted"
                    except Exception as exc:
                        collected = CollectedActionResult(status="failed")
                        artifacts = {}
                        facts = {}
                        error = f"{type(exc).__name__}: {exc}"
            finished = _utc_now()
            action_result = {
                "schema": 1,
                "contract_kind": "action-result",
                "node": node.node_id,
                "result_status": result_status,
                "execution": {
                    "status": execution.status,
                    "exit_code": execution.exit_code,
                    "started_at": started,
                    "finished_at": finished,
                    "details": json_value(execution.details),
                },
                "artifacts": {
                    role: self._artifact_payload(artifact, run_root)
                    for role, artifact in sorted(artifacts.items())
                },
                "facts": json_value(facts),
                "evidence": [
                    self._managed_relative(path, run_root, "Evidence")
                    for path in collected.evidence
                ],
                "details": json_value(collected.details),
                "error": error,
                "content_fingerprint": self._content_fingerprint(artifacts, facts),
                "execution_fingerprint": execution_fingerprint,
            }
            atomic_write_json(node_root / "action_result.json", action_result)
            policy = None if node.policy is None else plan.spec.policy(node.policy)
            evaluation = (
                evaluate_policy(policy, facts)
                if result_status == "valid"
                else EvaluatedPolicy(
                    None if policy is None else policy.policy_id,
                    "not-evaluated",
                    (),
                )
            )
            self._write_policy_receipt(node_root, evaluation, policy)
            node_status = (
                evaluation.status
                if result_status == "valid"
                else "failed"
            )
            outcome = NodeOutcome(
                node_id=node.node_id,
                status=node_status,
                execution_status=execution.status,
                result_status=result_status,
                policy_status=evaluation.status,
                artifacts=MappingProxyType(dict(artifacts)),
                facts=MappingProxyType(dict(facts)),
                reason=error,
                execution_fingerprint=execution_fingerprint,
            )
            outcomes[node.node_id] = outcome
            atomic_write_json(
                node_root / "run_manifest.json",
                {
                    "schema": 1,
                    "contract_kind": "action-run-manifest",
                    "node": node.node_id,
                    "execution_fingerprint": execution_fingerprint,
                    "managed_paths": [
                        self._managed_relative(path, run_root, "managed path")
                        for path in sorted(node_root.rglob("*"))
                    ],
                },
            )

        flow_status = (
            "accepted"
            if all(outcomes[goal].status == "accepted" for goal in plan.target.goals)
            else "failed"
        )
        flow_payload = {
            "schema": 1,
            "contract_kind": "flow-result",
            "owner": plan.spec.owner,
            "flow": plan.spec.flow_id,
            "target": plan.target.target_id,
            "run_id": identity,
            "status": flow_status,
            "interrupted": interrupted,
            "topology": list(plan.topology),
            "nodes": {
                node_id: self._outcome_payload(outcome, run_root)
                for node_id, outcome in outcomes.items()
            },
            "plan_fingerprint": plan.fingerprint,
            "preflight_fingerprint": preflight.fingerprint,
        }
        atomic_write_json(run_root / "flow_result.json", flow_payload)
        atomic_write_json(
            run_root / "run_manifest.json",
            {
                "schema": 1,
                "contract_kind": "flow-run-manifest",
                "owner": plan.spec.owner,
                "flow": plan.spec.flow_id,
                "target": plan.target.target_id,
                "run_id": identity,
                "managed_paths": [
                    self._managed_relative(path, run_root, "managed path")
                    for path in sorted(run_root.rglob("*"))
                    if path != run_root / "run_manifest.json"
                ],
            },
        )
        return FlowResult(
            owner=plan.spec.owner,
            flow_id=plan.spec.flow_id,
            target=plan.target.target_id,
            run_id=identity,
            run_root=run_root,
            status=flow_status,
            interrupted=interrupted,
            nodes=MappingProxyType(outcomes),
        )

    def clean_run(
        self,
        *,
        artifact_root: Path,
        owner: str,
        flow_id: str,
        run_id: str,
    ) -> None:
        """Remove exactly one completed run, failing closed on manifest drift."""

        owner_id = owner_identity(owner, "Flow owner")
        flow_identity = identifier(flow_id, "Flow identity")
        identity = run_identity(run_id)
        store_root = Path(artifact_root).resolve()
        if store_root == Path(store_root.anchor):
            raise FlowExecutionError("artifact root cannot be a filesystem root")
        run_root = (
            store_root
            / "flows"
            / owner_id
            / flow_identity
            / "runs"
            / identity
        )
        resolved_run = run_root.resolve(strict=False)
        if not resolved_run.is_relative_to(store_root):
            raise FlowExecutionError("Flow Run path escaped the artifact root")
        if not run_root.is_dir() or run_root.is_symlink():
            raise FlowExecutionError(f"cannot clean missing or unsafe Flow Run: {identity}")

        manifest_path = run_root / "run_manifest.json"
        try:
            manifest = read_json_object(manifest_path, "Flow Run Manifest")
        except (OSError, RuntimeError) as exc:
            raise FlowExecutionError(str(exc)) from exc
        if (
            manifest.get("schema") != 1
            or manifest.get("contract_kind") != "flow-run-manifest"
            or manifest.get("owner") != owner_id
            or manifest.get("flow") != flow_identity
            or manifest.get("run_id") != identity
        ):
            raise FlowExecutionError("Flow Run Manifest identity does not match clean target")
        raw_paths = manifest.get("managed_paths")
        if not isinstance(raw_paths, list) or any(
            not isinstance(value, str) for value in raw_paths
        ):
            raise FlowExecutionError("Flow Run Manifest has invalid managed paths")
        if len(raw_paths) != len(set(raw_paths)):
            raise FlowExecutionError("Flow Run Manifest repeats a managed path")

        declared: dict[str, Path] = {}
        for relative in raw_paths:
            relative_path = Path(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in relative_path.parts)
            ):
                raise FlowExecutionError(
                    f"unsafe managed path in Flow Run Manifest: {relative!r}"
                )
            candidate = (run_root / relative_path).resolve(strict=False)
            if candidate == resolved_run or not candidate.is_relative_to(resolved_run):
                raise FlowExecutionError(
                    f"unsafe managed path in Flow Run Manifest: {relative!r}"
                )
            declared[relative_path.as_posix()] = candidate

        actual: dict[str, Path] = {}
        for path in run_root.rglob("*"):
            relative = path.relative_to(run_root).as_posix()
            if relative == "run_manifest.json":
                continue
            if path.is_symlink():
                raise FlowExecutionError(
                    f"refusing to clean symlink in Flow Run: {relative!r}"
                )
            actual[relative] = path.resolve(strict=False)
        missing = sorted(set(declared) - set(actual))
        untracked = sorted(set(actual) - set(declared))
        if missing or untracked:
            raise FlowExecutionError(
                "Flow Run manifest drift: "
                f"missing={missing}, untracked={untracked}"
            )

        for relative in sorted(declared, key=lambda value: len(Path(value).parts), reverse=True):
            path = declared[relative]
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        manifest_path.unlink()
        run_root.rmdir()

    def read_run_result(
        self,
        *,
        artifact_root: Path,
        owner: str,
        flow_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Read one current-schema result after checking its selected identity."""

        owner_id = owner_identity(owner, "Flow owner")
        flow_identity = identifier(flow_id, "Flow identity")
        identity = run_identity(run_id)
        store_root = Path(artifact_root).resolve()
        if store_root == Path(store_root.anchor):
            raise FlowExecutionError("artifact root cannot be a filesystem root")
        run_root = (
            store_root
            / "flows"
            / owner_id
            / flow_identity
            / "runs"
            / identity
        )
        if not run_root.resolve(strict=False).is_relative_to(store_root):
            raise FlowExecutionError("Flow Run path escaped the artifact root")
        try:
            result = read_json_object(run_root / "flow_result.json", "Flow Result")
        except (OSError, RuntimeError) as exc:
            raise FlowExecutionError(str(exc)) from exc
        if (
            result.get("schema") != 1
            or result.get("contract_kind") != "flow-result"
            or result.get("owner") != owner_id
            or result.get("flow") != flow_identity
            or result.get("run_id") != identity
        ):
            raise FlowExecutionError("Flow Result identity does not match selected run")
        return result

    def _block_reason(
        self,
        node: Any,
        outcomes: Mapping[str, NodeOutcome],
    ) -> str | None:
        binding_producers = {binding.producer for binding in node.bindings}
        for binding in node.bindings:
            producer = outcomes[binding.producer]
            satisfied = (
                producer.status == "accepted"
                if binding.requires == "accepted"
                else producer.result_status == "valid"
            )
            if not satisfied:
                return (
                    f"input {binding.input!r} requires {binding.requires} result "
                    f"from {binding.producer!r}"
                )
        for predecessor in node.order_after:
            if predecessor not in binding_producers and outcomes[predecessor].status != "accepted":
                return f"ordering predecessor {predecessor!r} was not accepted"
        return None

    def _materialize_inputs(
        self,
        node: Any,
        outcomes: Mapping[str, NodeOutcome],
    ) -> dict[str, InputArtifact]:
        inputs: dict[str, InputArtifact] = {}
        for binding in node.bindings:
            artifact = outcomes[binding.producer].artifacts[binding.output]
            inputs[binding.input] = InputArtifact(
                role=binding.input,
                kind=artifact.kind,
                path=artifact.path,
                digest=artifact.digest,
                producer=binding.producer,
                qualifiers=artifact.qualifiers,
            )
        return inputs

    def _validate_collected(
        self,
        context: ActionContext,
        collected: CollectedActionResult,
        *,
        run_root: Path,
    ) -> dict[str, ActionArtifact]:
        artifacts: dict[str, ActionArtifact] = {}
        for produced in collected.artifacts:
            if produced.role in artifacts:
                raise FlowExecutionError(
                    f"Action {context.node_id!r} emitted role {produced.role!r} twice"
                )
            port = context.action.output(produced.role)
            if produced.kind != port.kind:
                raise FlowExecutionError(
                    f"output {produced.role!r} kind {produced.kind!r} does not match "
                    f"contract kind {port.kind!r}"
                )
            path = Path(produced.path).resolve()
            if not path.is_file() or not path.is_relative_to(context.output_root.resolve()):
                raise FlowExecutionError(
                    f"output {produced.role!r} is not a managed regular file"
                )
            artifacts[produced.role] = ActionArtifact(
                role=produced.role,
                kind=produced.kind,
                path=path,
                relative_path=self._managed_relative(path, run_root, "output"),
                digest=_sha256(path),
                producer=context.node_id,
                qualifiers=produced.qualifiers,
            )
        if collected.status == "valid":
            missing = {
                port.role
                for port in context.action.outputs
                if port.required and port.role not in artifacts
            }
            if missing:
                raise FlowExecutionError(
                    f"Action {context.node_id!r} omitted outputs {sorted(missing)}"
                )
        for evidence in collected.evidence:
            path = Path(evidence).resolve()
            if not path.is_file() or not path.is_relative_to(context.node_root.resolve()):
                raise FlowExecutionError("Evidence is not a managed regular file")
        return artifacts

    def _reusable_outcome(
        self,
        node_root: Path,
        *,
        run_root: Path,
        node_id: str,
        interface_fingerprint: str,
        execution_fingerprint: str,
    ) -> NodeOutcome | None:
        request_path = node_root / "action_request.json"
        result_path = node_root / "action_result.json"
        if not request_path.is_file() or not result_path.is_file():
            return None
        try:
            request = read_json_object(request_path, "Action Request")
            result = read_json_object(result_path, "Action Result")
        except (OSError, RuntimeError):
            return None
        if (
            request.get("schema") != 1
            or request.get("contract_kind") != "action-request"
            or request.get("interface_fingerprint") != interface_fingerprint
            or request.get("execution_fingerprint") != execution_fingerprint
            or result.get("schema") != 1
            or result.get("contract_kind") != "action-result"
            or result.get("result_status") != "valid"
            or result.get("execution_fingerprint") != execution_fingerprint
        ):
            return None
        raw_artifacts = result.get("artifacts")
        raw_facts = result.get("facts")
        if not isinstance(raw_artifacts, dict) or not isinstance(raw_facts, dict):
            return None
        artifacts: dict[str, ActionArtifact] = {}
        for role, value in raw_artifacts.items():
            if not isinstance(value, dict):
                return None
            relative = value.get("path")
            digest = value.get("digest")
            kind = value.get("kind")
            qualifiers = value.get("qualifiers")
            if not all(isinstance(item, str) and item for item in (relative, digest, kind)):
                return None
            if not isinstance(qualifiers, dict):
                return None
            path = (run_root / relative).resolve()
            if not path.is_relative_to(run_root.resolve()) or not path.is_file():
                return None
            if _sha256(path) != digest:
                return None
            artifacts[role] = ActionArtifact(
                role=role,
                kind=kind,
                path=path,
                relative_path=relative,
                digest=digest,
                producer=node_id,
                qualifiers=qualifiers,
            )
        if result.get("content_fingerprint") != self._content_fingerprint(
            artifacts,
            raw_facts,
        ):
            return None
        return NodeOutcome(
            node_id=node_id,
            status="accepted",
            execution_status="succeeded",
            result_status="valid",
            policy_status=None,
            artifacts=MappingProxyType(artifacts),
            facts=MappingProxyType(dict(raw_facts)),
            reused=True,
            execution_fingerprint=execution_fingerprint,
        )

    def _write_policy_receipt(
        self,
        node_root: Path,
        evaluation: EvaluatedPolicy,
        policy: PolicySpec | None,
    ) -> None:
        atomic_write_json(
            node_root / "policy_receipt.json",
            {
                "schema": 1,
                "contract_kind": "policy-receipt",
                "policy": evaluation.policy_id,
                "policy_fingerprint": (
                    None if policy is None else canonical_digest(policy)
                ),
                "status": evaluation.status,
                "checks": [json_value(check) for check in evaluation.checks],
            },
        )

    def _content_fingerprint(
        self,
        artifacts: Mapping[str, ActionArtifact],
        facts: Mapping[str, Any],
    ) -> str:
        return canonical_digest(
            {
                "artifacts": {
                    role: {
                        "kind": artifact.kind,
                        "digest": artifact.digest,
                        "qualifiers": json_value(artifact.qualifiers),
                    }
                    for role, artifact in sorted(artifacts.items())
                },
                "facts": json_value(facts),
            }
        )

    def _remove_managed_node(self, node_root: Path, run_root: Path) -> None:
        resolved_node = node_root.resolve()
        resolved_nodes = (run_root / "nodes").resolve()
        if (
            resolved_node == resolved_nodes
            or not resolved_node.is_relative_to(resolved_nodes)
            or node_root.is_symlink()
        ):
            raise FlowExecutionError("refusing to replace an unsafe node directory")
        manifest_path = node_root / "run_manifest.json"
        try:
            manifest = read_json_object(manifest_path, "Action Run Manifest")
        except (OSError, RuntimeError) as exc:
            raise FlowExecutionError(str(exc)) from exc
        if (
            manifest.get("schema") != 1
            or manifest.get("contract_kind") != "action-run-manifest"
            or manifest.get("node") != node_root.name
        ):
            raise FlowExecutionError("Action Run Manifest identity does not match node")
        raw_paths = manifest.get("managed_paths")
        if not isinstance(raw_paths, list) or any(
            not isinstance(value, str) for value in raw_paths
        ):
            raise FlowExecutionError("Action Run Manifest has invalid managed paths")
        if len(raw_paths) != len(set(raw_paths)):
            raise FlowExecutionError("Action Run Manifest repeats a managed path")

        declared: dict[str, Path] = {}
        for relative in raw_paths:
            relative_path = Path(relative)
            candidate = (run_root / relative_path).resolve(strict=False)
            if (
                not relative
                or relative_path.is_absolute()
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or not candidate.is_relative_to(resolved_node)
            ):
                raise FlowExecutionError(
                    f"unsafe managed path in Action Run Manifest: {relative!r}"
                )
            declared[relative_path.as_posix()] = candidate

        actual: dict[str, Path] = {}
        for path in node_root.rglob("*"):
            relative = path.relative_to(run_root).as_posix()
            if path == manifest_path:
                continue
            if path.is_symlink():
                raise FlowExecutionError(
                    f"refusing to replace symlink in Action Run: {relative!r}"
                )
            actual[relative] = path.resolve(strict=False)
        missing = sorted(set(declared) - set(actual))
        untracked = sorted(set(actual) - set(declared))
        if missing or untracked:
            raise FlowExecutionError(
                "Action Run manifest drift: "
                f"missing={missing}, untracked={untracked}"
            )
        for relative in sorted(
            declared,
            key=lambda value: len(Path(value).parts),
            reverse=True,
        ):
            path = declared[relative]
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        manifest_path.unlink()
        node_root.rmdir()

    def _managed_relative(self, path: Path, run_root: Path, label: str) -> str:
        candidate = Path(path).resolve()
        root = run_root.resolve()
        if not candidate.is_relative_to(root):
            raise FlowExecutionError(f"{label} escaped the Flow Run")
        return candidate.relative_to(root).as_posix()

    def _artifact_payload(
        self,
        artifact: InputArtifact | ActionArtifact,
        run_root: Path,
    ) -> dict[str, Any]:
        return {
            "kind": artifact.kind,
            "path": self._managed_relative(artifact.path, run_root, "artifact"),
            "digest": artifact.digest,
            "producer": artifact.producer,
            "qualifiers": json_value(artifact.qualifiers),
        }

    def _environment_payload(
        self,
        planned: PlannedNode,
        environment: ExecutionEnvironment,
    ) -> dict[str, Any]:
        platform_assets: dict[str, dict[str, str]] = {}
        for role, asset in self._resolved_platform_assets(
            planned,
            environment,
        ).items():
            platform_assets[role] = {
                "kind": asset.kind,
                "identity": asset.identity,
                "digest": asset.digest,
            }
        return {
            "capabilities": {
                capability: environment.capabilities[capability].identity
                for capability in planned.required_capabilities
            },
            "platform_assets": platform_assets,
        }

    def _resolved_platform_assets(
        self,
        planned: PlannedNode,
        environment: ExecutionEnvironment,
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for requirement in planned.platform_assets:
            asset = environment.platform_asset(requirement.role)
            if asset is None or asset.kind != requirement.kind:
                raise FlowExecutionError(
                    f"preflight did not resolve platform asset {requirement.role!r}"
                )
            resolved[requirement.role] = asset
        return resolved

    def _outcome_payload(self, outcome: NodeOutcome, run_root: Path) -> dict[str, Any]:
        return {
            "status": outcome.status,
            "execution_status": outcome.execution_status,
            "result_status": outcome.result_status,
            "policy_status": outcome.policy_status,
            "reused": outcome.reused,
            "reason": outcome.reason,
            "execution_fingerprint": outcome.execution_fingerprint,
            "artifacts": {
                role: self._artifact_payload(artifact, run_root)
                for role, artifact in sorted(outcome.artifacts.items())
            },
            "facts": json_value(outcome.facts),
        }
