"""Deterministic planning and managed local execution for typed design Flows."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
import uuid

from sigilicon.artifacts import atomic_write_json, read_json_object, read_nofollow_text
from sigilicon.flow.environment import capability_available
from sigilicon.flow.model import (
    ActionArtifact,
    ActionContext,
    ActionContract,
    AdapterExecution,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowContractError,
    FlowExecutionError,
    FlowPlan,
    FlowProgress,
    FlowResult,
    FlowSpec,
    InputArtifact,
    NodeOutcome,
    PlatformAssetRequirement,
    PlannedNode,
    PreflightCheck,
    PreflightResult,
    ProducedArtifact,
    SourceMember,
    identifier,
    owner_identity,
    run_identity,
)
from sigilicon.flow.policy import EvaluatedPolicy, evaluate_policy
from sigilicon.flow.registry import FlowRegistry, ToolAdapter
from sigilicon.flow.serialization import json_value
from sigilicon.flow.source_assets import (
    git_source,
    resolve_node_source_assets,
    source_assets_payload,
    source_member_matches,
)
from sigilicon.paths import ArtifactLayout, ProjectScope


_NODE_EXTENSION_RESERVED = frozenset(
    {
        "id",
        "action",
        "adapter",
        "action_config",
        "adapter_config",
        "required_capabilities",
        "execution_capability",
        "platform_assets",
        "policy",
        "dependencies",
        "bindings",
        "source_assets",
    }
)
_REQUEST_EXTENSION_RESERVED = frozenset(
    {
        "schema",
        "contract_kind",
        "node",
        "action",
        "adapter",
        "action_config",
        "adapter_config",
        "execution_environment",
        "inputs",
        "source_assets",
    }
)
_RESERVED_EXTENSIONS = _NODE_EXTENSION_RESERVED | _REQUEST_EXTENSION_RESERVED


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_payload(
    spec: FlowSpec,
    profile: ExecutionProfile,
    target_id: str,
    planned: tuple[PlannedNode, ...],
    topology: tuple[str, ...],
    implementation_sources: tuple[SourceMember, ...] = (),
) -> dict[str, Any]:
    def node_payload(item: PlannedNode) -> dict[str, Any]:
        value = {
            "id": item.node.node_id,
            "action": item.node.action_kind,
            "adapter": item.adapter,
            "action_config": json_value(item.node.config),
            "adapter_config": json_value(item.adapter_config),
            "required_capabilities": list(item.required_capabilities),
            "execution_capability": item.execution_capability,
            "platform_assets": [
                json_value(requirement) for requirement in item.platform_assets
            ],
            "policy": item.node.policy,
            "dependencies": list(item.dependencies),
            "bindings": [json_value(binding) for binding in item.node.bindings],
            "source_assets": (
                None
                if item.source_assets is None
                else source_assets_payload(item.source_assets)
            ),
        }
        value.update(json_value(item.node.extensions))
        return value

    payload = {
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
        "nodes": [node_payload(item) for item in planned],
        "policies": [json_value(policy) for policy in spec.policies],
    }
    if implementation_sources:
        payload["implementation_sources"] = [
            {
                "path": source.path,
                "sha256": hashlib.sha256(
                    source.record_text.encode("utf-8")
                ).hexdigest(),
                "executable": source.executable,
            }
            for source in implementation_sources
        ]
    return payload


class FlowEngine:
    """Deep module that plans typed graphs and executes them through Adapters."""

    def __init__(
        self,
        registry: FlowRegistry,
        *,
        project_scope: ProjectScope | None = None,
    ) -> None:
        self._registry = registry
        self._project_scope = project_scope

    def planned_output(self, plan: FlowPlan, node_id: str, role: str) -> ArtifactPort:
        """Resolve one output through the exact Action registry used by this engine."""

        planned = plan.planned_node(node_id)
        return self._registry.action(planned.node.action_kind).output(role)

    @staticmethod
    def _validate_extensions(
        contract: ActionContract,
        adapter: ToolAdapter,
        names: tuple[str, ...],
    ) -> None:
        reserved = set(names) & _RESERVED_EXTENSIONS
        if reserved:
            raise FlowContractError(
                f"Flow extensions use reserved fields: {sorted(reserved)}"
            )
        accepted_by_action = set(getattr(contract, "accepted_extensions", ()))
        unsupported_action = set(names) - accepted_by_action
        if unsupported_action:
            raise FlowContractError(
                "Action does not accept Flow extensions: "
                f"{sorted(unsupported_action)}"
            )
        accepted_by_adapter = getattr(adapter, "accepted_extensions", ())
        if not isinstance(accepted_by_adapter, tuple) or any(
            not isinstance(name, str) for name in accepted_by_adapter
        ):
            raise FlowContractError("Adapter accepted_extensions must be a tuple")
        for name in accepted_by_adapter:
            identifier(name, "Adapter extension")
        if len(accepted_by_adapter) != len(set(accepted_by_adapter)):
            raise FlowContractError("duplicate Adapter extensions")
        unsupported_adapter = set(names) - set(accepted_by_adapter)
        if unsupported_adapter:
            raise FlowContractError(
                "Adapter does not consume Flow extensions: "
                f"{sorted(unsupported_adapter)}"
            )

    def validate_extensions(
        self,
        plan: FlowPlan,
        node_id: str,
        names: tuple[str, ...],
    ) -> None:
        """Validate opaque extension names without interpreting their payloads."""

        planned = plan.planned_node(node_id)
        self._validate_extensions(
            self._registry.action(planned.node.action_kind),
            self._registry.adapter(planned.adapter),
            names,
        )

    @staticmethod
    def plan_id(plan: FlowPlan) -> str:
        """Return the owner-declared semantic selector for one resolved plan.

        The selector is not a correctness projection. Authorization and replay
        compare :meth:`plan_record` in full whenever this selector is resolved.
        """

        return (
            f"{plan.spec.owner}:{plan.spec.flow_id}:"
            f"{plan.target.target_id}:{plan.profile.profile_id}"
        )

    def plan(
        self,
        spec: FlowSpec,
        target_id: str,
        profile: ExecutionProfile,
    ) -> FlowPlan:
        if self._project_scope is not None and spec.owner != self._project_scope.owner:
            raise FlowContractError(
                f"Flow owner {spec.owner!r} does not match explicit project owner "
                f"{self._project_scope.owner!r}"
            )
        if profile.owner != spec.owner:
            raise FlowContractError(
                f"Execution Profile owner {profile.owner!r} does not match "
                f"Flow owner {spec.owner!r}"
            )
        target = spec.target(target_id)
        node_order = {node.node_id: index for index, node in enumerate(spec.nodes)}
        dependencies: dict[str, tuple[str, ...]] = {}
        source_assets: dict[str, Any] = {}
        for node in spec.nodes:
            contract = self._registry.action(node.action_kind)
            selection = profile.selection(node.action_kind)
            if selection.adapter not in contract.adapters:
                raise FlowContractError(
                    f"Adapter {selection.adapter!r} cannot implement "
                    f"Action {contract.kind!r}"
                )
            self._validate_extensions(
                contract,
                self._registry.adapter(selection.adapter),
                tuple(node.extensions),
            )
            source_assets[node.node_id] = resolve_node_source_assets(
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
            contract_asset_roles = {
                requirement.role for requirement in contract.platform_assets
            }
            unknown_asset_identities = (
                set(selection.platform_asset_identities) - contract_asset_roles
            )
            if unknown_asset_identities:
                raise FlowContractError(
                    f"Execution Profile action {node.action_kind!r} selects unknown "
                    "platform asset identities: "
                    f"{sorted(unknown_asset_identities)}"
                )
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
                    platform_assets=tuple(
                        PlatformAssetRequirement(
                            requirement.role,
                            requirement.kind,
                            requirement.members,
                            selection.platform_asset_identities.get(
                                requirement.role
                            ),
                        )
                        for requirement in contract.platform_assets
                    ),
                    dependencies=dependencies[node_id],
                    execution_capability=contract.execution_capability,
                    source_assets=source_assets[node_id],
                )
            )
        planned = tuple(planned_items)
        topology_tuple = tuple(topology)
        return FlowPlan(
            spec=spec,
            profile=profile,
            target=target,
            nodes=planned,
            topology=topology_tuple,
        )

    def plan_record(self, plan: FlowPlan) -> dict[str, Any]:
        """Return the canonical, source-only record for a resolved plan."""

        return _plan_payload(
            plan.spec,
            plan.profile,
            plan.target.target_id,
            plan.nodes,
            plan.topology,
            self._registry.implementation_sources,
        )

    def preflight(
        self,
        plan: FlowPlan,
        environment: ExecutionEnvironment,
    ) -> PreflightResult:
        """Purely compare planned semantic requirements with current site facts."""

        checks: list[PreflightCheck] = []
        for source in self._registry.implementation_sources:
            try:
                exact_source = source_member_matches(source)
            except (OSError, RuntimeError, UnicodeError):
                exact_source = False
            checks.append(
                PreflightCheck(
                    requirement=source.path,
                    requirement_kind="implementation-source",
                    status="available" if exact_source else "changed",
                    expected=hashlib.sha256(
                        source.record_text.encode("utf-8")
                    ).hexdigest(),
                )
            )
        seen_adapters: set[str] = set()
        seen_capabilities: set[str] = set()
        seen_assets: set[tuple[str, str, tuple[str, ...], str | None]] = set()
        for planned in plan.nodes:
            if planned.adapter not in seen_adapters:
                seen_adapters.add(planned.adapter)
                available = self._registry.has_adapter(planned.adapter)
                checks.append(
                    PreflightCheck(
                        requirement=planned.adapter,
                        requirement_kind="adapter",
                        status="available" if available else "missing",
                    )
                )
            if planned.source_assets is not None:
                current_source = git_source(planned.source_assets.owner_root)
                try:
                    exact_members = all(
                        source_member_matches(member)
                        for artifact in planned.source_assets.artifacts
                        for member in artifact.members
                    )
                except (OSError, RuntimeError, UnicodeError):
                    exact_members = False
                checks.append(
                    PreflightCheck(
                        requirement=planned.source_assets.name,
                        requirement_kind="git-source",
                        status=(
                            "available"
                            if current_source == planned.source_assets.git
                            and exact_members
                            else "changed"
                        ),
                        expected=planned.source_assets.git.commit,
                        identity=current_source.commit,
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
                    requirement.identity,
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
                    or (
                        requirement.identity is not None
                        and asset.identity != requirement.identity
                    )
                    else "incomplete"
                    if missing_members
                    else "available"
                )
                checks.append(
                    PreflightCheck(
                        requirement=requirement.role,
                        requirement_kind="platform-asset",
                        status=status,
                        expected=(
                            requirement.kind
                            if requirement.identity is None
                            else {
                                "kind": requirement.kind,
                                "identity": requirement.identity,
                            }
                        ),
                        identity=None if asset is None else asset.identity,
                    )
                )
        status = (
            "ready"
            if all(check.status == "available" for check in checks)
            else "blocked"
        )
        return PreflightResult(status, tuple(checks))

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
        }

    def run(
        self,
        plan: FlowPlan,
        *,
        artifact_root: Path,
        environment: ExecutionEnvironment | None = None,
        run_id: str | None = None,
        progress: Callable[[FlowProgress], None] | None = None,
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
        run_paths = ArtifactLayout(store_root).execution(
            owner=plan.spec.owner,
            target=plan.target.target_id,
            flow=plan.spec.flow_id,
            variant="default",
            identity=identity,
            artifact_kind="flow",
            identity_kind="run_id",
        )
        run_root = run_paths.root
        if not run_root.resolve(strict=False).is_relative_to(store_root):
            raise FlowExecutionError("Flow Run path escaped the artifact root")
        if run_root.exists():
            raise FlowExecutionError(f"Flow Run already exists: {identity}")
        run_paths.create()

        def notify(status: str, completed: int, current: str | None) -> None:
            if progress is not None:
                progress(
                    FlowProgress(
                        identity,
                        status,
                        completed,
                        len(plan.nodes),
                        current,
                    )
                )

        atomic_write_json(run_paths.role("inputs") / "resolved_plan.json", self.plan_record(plan))
        atomic_write_json(
            run_paths.role("inputs") / "preflight.json",
            self.preflight_record(plan, preflight),
        )

        outcomes: dict[str, NodeOutcome] = {}
        interrupted = False
        notify("running", 0, None)
        for planned in plan.nodes:
            node = planned.node
            notify("running", len(outcomes), node.node_id)
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
                notify("running", len(outcomes), None)
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
                notify("running", len(outcomes), None)
                continue
            inputs = self._materialize_inputs(node, outcomes)
            action = self._registry.action(node.action_kind)
            adapter = self._registry.adapter(planned.adapter)
            environment_payload = self._environment_payload(
                planned,
                current_environment,
            )
            input_root = run_paths.role("inputs") / node.node_id
            work_root = run_paths.role("work") / node.node_id
            output_root = run_paths.role("outputs") / node.node_id
            log_root = run_paths.role("logs") / node.node_id
            input_root.mkdir()
            work_root.mkdir()
            output_root.mkdir()
            log_root.mkdir()
            context = ActionContext(
                node_id=node.node_id,
                action=action,
                run_root=run_root,
                work_root=work_root,
                output_root=output_root,
                log_root=log_root,
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
                source_assets=planned.source_assets,
                extensions=node.extensions,
                project_scope=self._project_scope,
            )
            request = {
                "schema": 1,
                "contract_kind": "action-request",
                "node": node.node_id,
                "action": node.action_kind,
                "adapter": planned.adapter,
                "action_config": json_value(node.config),
                "adapter_config": json_value(planned.adapter_config),
                "execution_environment": environment_payload,
                "inputs": {
                    role: self._artifact_payload(artifact, run_root)
                    for role, artifact in sorted(inputs.items())
                },
                "source_assets": (
                    None
                    if planned.source_assets is None
                    else source_assets_payload(planned.source_assets)
                ),
            }
            request.update(json_value(node.extensions))
            atomic_write_json(input_root / "action_request.json", request)
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
                        unknown_facts = set(facts) - (
                            set(action.facts) | set(action.optional_facts)
                        )
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
            }
            atomic_write_json(output_root / "action_result.json", action_result)
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
            self._write_policy_receipt(output_root, evaluation)
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
            )
            outcomes[node.node_id] = outcome
            notify("running", len(outcomes), None)
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
        }
        atomic_write_json(run_paths.role("outputs") / "flow_result.json", flow_payload)
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
                    self._inventory_relative(path, run_root)
                    for path in sorted(run_root.rglob("*"))
                    if path != run_root / "run_manifest.json"
                ],
            },
        )
        notify(
            "cancelled" if interrupted else flow_status,
            len(outcomes),
            None,
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
        target: str,
        run_id: str,
    ) -> None:
        """Remove exactly one completed run, failing closed on manifest drift."""

        owner_id = owner_identity(owner, "Flow owner")
        flow_identity = identifier(flow_id, "Flow identity")
        target_identity = identifier(target, "Flow target")
        identity = run_identity(run_id)
        store_root = Path(artifact_root).resolve()
        if store_root == Path(store_root.anchor):
            raise FlowExecutionError("artifact root cannot be a filesystem root")
        run_paths = ArtifactLayout(store_root).execution(
            owner=owner_id,
            target=target_identity,
            flow=flow_identity,
            variant="default",
            identity=identity,
            artifact_kind="flow",
            identity_kind="run_id",
        )
        run_root = run_paths.root
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
            or manifest.get("target") != target_identity
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
            declared[relative_path.as_posix()] = run_root / relative_path

        actual: dict[str, Path] = {}
        for path in run_root.rglob("*"):
            relative = path.relative_to(run_root).as_posix()
            if relative == "run_manifest.json":
                continue
            if path.is_symlink() and not path.resolve(strict=False).is_relative_to(
                resolved_run
            ):
                raise FlowExecutionError(
                    f"refusing to clean escaping symlink in Flow Run: {relative!r}"
                )
            actual[relative] = path
        missing = sorted(set(declared) - set(actual))
        untracked = sorted(set(actual) - set(declared))
        if missing or untracked:
            raise FlowExecutionError(
                "Flow Run manifest drift: "
                f"missing={missing}, untracked={untracked}"
            )

        for relative in sorted(declared, key=lambda value: len(Path(value).parts), reverse=True):
            path = declared[relative]
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
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
        target: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Read one current-schema result after checking its selected identity."""

        owner_id = owner_identity(owner, "Flow owner")
        flow_identity = identifier(flow_id, "Flow identity")
        target_identity = identifier(target, "Flow target")
        identity = run_identity(run_id)
        store_root = Path(artifact_root).resolve()
        if store_root == Path(store_root.anchor):
            raise FlowExecutionError("artifact root cannot be a filesystem root")
        run_paths = ArtifactLayout(store_root).execution(
            owner=owner_id,
            target=target_identity,
            flow=flow_identity,
            variant="default",
            identity=identity,
            artifact_kind="flow",
            identity_kind="run_id",
        )
        run_root = run_paths.root
        if not run_root.resolve(strict=False).is_relative_to(store_root):
            raise FlowExecutionError("Flow Run path escaped the artifact root")
        try:
            result = read_json_object(
                run_paths.role("outputs") / "flow_result.json",
                "Flow Result",
            )
        except (OSError, RuntimeError) as exc:
            raise FlowExecutionError(str(exc)) from exc
        if (
            result.get("schema") != 1
            or result.get("contract_kind") != "flow-result"
            or result.get("owner") != owner_id
            or result.get("flow") != flow_identity
            or result.get("target") != target_identity
            or result.get("run_id") != identity
        ):
            raise FlowExecutionError("Flow Result identity does not match selected run")
        return result

    def restore_result(
        self,
        plan: FlowPlan,
        *,
        artifact_root: Path,
        run_id: str,
    ) -> FlowResult:
        """Restore one complete persisted result against its exact typed plan.

        A run identity only selects the directory.  Correctness comes from the
        exact resolved-plan record, the closed run inventory, and strict typed
        reconstruction of every node and artifact field.
        """

        identity = run_identity(run_id)
        store_root = Path(artifact_root).resolve()
        if store_root == Path(store_root.anchor):
            raise FlowExecutionError("artifact root cannot be a filesystem root")
        run_paths = ArtifactLayout(store_root).execution(
            owner=plan.spec.owner,
            target=plan.target.target_id,
            flow=plan.spec.flow_id,
            variant="default",
            identity=identity,
            artifact_kind="flow",
            identity_kind="run_id",
        )
        run_root = run_paths.root
        if (
            not run_root.is_dir()
            or run_root.is_symlink()
            or not run_root.resolve().is_relative_to(store_root)
        ):
            raise FlowExecutionError(f"missing or unsafe persisted Flow Run: {identity}")
        try:
            manifest = read_json_object(run_root / "run_manifest.json", "Flow Run Manifest")
            resolved_plan = read_json_object(
                run_paths.role("inputs") / "resolved_plan.json",
                "Resolved Flow Plan",
            )
            record = read_json_object(
                run_paths.role("outputs") / "flow_result.json",
                "Flow Result",
            )
        except (OSError, RuntimeError) as exc:
            raise FlowExecutionError(str(exc)) from exc
        if set(manifest) != {
            "schema", "contract_kind", "owner", "flow", "target", "run_id",
            "managed_paths",
        } or (
            manifest["schema"] != 1
            or manifest["contract_kind"] != "flow-run-manifest"
            or manifest["owner"] != plan.spec.owner
            or manifest["flow"] != plan.spec.flow_id
            or manifest["target"] != plan.target.target_id
            or manifest["run_id"] != identity
        ):
            raise FlowExecutionError("Flow Run Manifest identity or fields drift")
        self._validate_run_inventory(run_root, manifest)
        if resolved_plan != self.plan_record(plan):
            raise FlowExecutionError("persisted resolved Flow Plan record drift")
        if set(record) != {
            "schema", "contract_kind", "owner", "flow", "target", "run_id",
            "status", "interrupted", "topology", "nodes",
        } or (
            record["schema"] != 1
            or record["contract_kind"] != "flow-result"
            or record["owner"] != plan.spec.owner
            or record["flow"] != plan.spec.flow_id
            or record["target"] != plan.target.target_id
            or record["run_id"] != identity
            or record["status"] not in {"accepted", "failed"}
            or type(record["interrupted"]) is not bool
            or record["topology"] != list(plan.topology)
            or not isinstance(record["nodes"], dict)
            or set(record["nodes"]) != set(plan.topology)
        ):
            raise FlowExecutionError("persisted Flow Result identity or fields drift")

        outcomes: dict[str, NodeOutcome] = {}
        for node_id in plan.topology:
            raw = record["nodes"][node_id]
            if not isinstance(raw, dict) or set(raw) != {
                "status", "execution_status", "result_status", "policy_status",
                "reason", "artifacts", "facts",
            }:
                raise FlowExecutionError("persisted Flow node fields drift")
            if (
                raw["status"] not in {"accepted", "rejected", "failed", "blocked"}
                or raw["execution_status"] not in {None, "succeeded", "failed", "cancelled"}
                or raw["result_status"] not in {None, "valid", "failed", "partial", "uncertain"}
                or raw["policy_status"] not in {None, "accepted", "rejected", "not-evaluated"}
                or (raw["reason"] is not None and not isinstance(raw["reason"], str))
                or not isinstance(raw["artifacts"], dict)
                or not isinstance(raw["facts"], dict)
            ):
                raise FlowExecutionError("persisted Flow node value drift")
            artifacts: dict[str, ActionArtifact] = {}
            contract = self._registry.action(plan.spec.node(node_id).action_kind)
            for role, payload in raw["artifacts"].items():
                if not isinstance(payload, dict) or set(payload) != {
                    "kind", "path", "producer", "qualifiers",
                }:
                    raise FlowExecutionError("persisted Flow artifact fields drift")
                try:
                    port = contract.output(role)
                except FlowContractError as exc:
                    raise FlowExecutionError(str(exc)) from exc
                relative_text = payload["path"]
                if not isinstance(relative_text, str):
                    raise FlowExecutionError("persisted Flow artifact path is invalid")
                relative = Path(relative_text)
                if (
                    not relative_text
                    or relative.is_absolute()
                    or "\\" in relative_text
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    raise FlowExecutionError("persisted Flow artifact path is unsafe")
                path = run_root / relative
                expected_root = run_paths.role("outputs") / node_id
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(expected_root.resolve())
                    or payload["producer"] != node_id
                    or payload["kind"] != port.kind
                ):
                    raise FlowExecutionError("persisted Flow artifact lineage drift")
                artifacts[role] = ActionArtifact(
                    role,
                    payload["kind"],
                    path,
                    relative.as_posix(),
                    payload["producer"],
                    payload["qualifiers"],
                )
            outcomes[node_id] = NodeOutcome(
                node_id,
                raw["status"],
                raw["execution_status"],
                raw["result_status"],
                raw["policy_status"],
                MappingProxyType(artifacts),
                MappingProxyType(dict(raw["facts"])),
                raw["reason"],
            )
        return FlowResult(
            plan.spec.owner,
            plan.spec.flow_id,
            plan.target.target_id,
            identity,
            run_root,
            record["status"],
            record["interrupted"],
            MappingProxyType(outcomes),
        )

    def _validate_run_inventory(
        self,
        run_root: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        """Fail closed unless a Flow Run Manifest exactly owns its inventory."""

        raw_paths = manifest.get("managed_paths")
        if not isinstance(raw_paths, list) or any(
            not isinstance(value, str) for value in raw_paths
        ):
            raise FlowExecutionError("Flow Run Manifest has invalid managed paths")
        if len(raw_paths) != len(set(raw_paths)):
            raise FlowExecutionError("Flow Run Manifest repeats a managed path")
        resolved_run = run_root.resolve()
        declared: set[str] = set()
        for relative_text in raw_paths:
            relative = Path(relative_text)
            if (
                not relative_text
                or relative.is_absolute()
                or "\\" in relative_text
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise FlowExecutionError(
                    f"unsafe managed path in Flow Run Manifest: {relative_text!r}"
                )
            candidate = (run_root / relative).resolve(strict=False)
            if candidate == resolved_run or not candidate.is_relative_to(resolved_run):
                raise FlowExecutionError(
                    f"unsafe managed path in Flow Run Manifest: {relative_text!r}"
                )
            declared.add(relative.as_posix())

        actual: set[str] = set()
        for path in run_root.rglob("*"):
            relative = path.relative_to(run_root).as_posix()
            if relative == "run_manifest.json":
                continue
            if path.is_symlink() and not path.resolve(strict=False).is_relative_to(
                resolved_run
            ):
                raise FlowExecutionError(
                    f"escaping symlink in Flow Run inventory: {relative!r}"
                )
            actual.add(relative)
        missing = sorted(declared - actual)
        untracked = sorted(actual - declared)
        if missing or untracked:
            raise FlowExecutionError(
                "Flow Run manifest drift: "
                f"missing={missing}, untracked={untracked}"
            )

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
            if binding.output not in producer.artifacts:
                return (
                    f"input {binding.input!r} requires artifact role "
                    f"{binding.output!r} from {binding.producer!r}"
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
            managed_roots = (
                context.work_root.resolve(),
                context.output_root.resolve(),
                context.log_root.resolve(),
            )
            if not path.is_file() or not any(
                path.is_relative_to(root) for root in managed_roots
            ):
                raise FlowExecutionError("Evidence is not a managed regular file")
        return artifacts

    def _write_policy_receipt(
        self,
        output_root: Path,
        evaluation: EvaluatedPolicy,
    ) -> None:
        atomic_write_json(
            output_root / "policy_receipt.json",
            {
                "schema": 1,
                "contract_kind": "policy-receipt",
                "policy": evaluation.policy_id,
                "status": evaluation.status,
                "checks": [json_value(check) for check in evaluation.checks],
            },
        )

    def _inventory_relative(self, path: Path, run_root: Path) -> str:
        """Record a managed locator without dereferencing tool-created symlinks."""

        candidate = Path(path).absolute()
        root = run_root.absolute()
        if candidate == root or not candidate.is_relative_to(root):
            raise FlowExecutionError("managed path escaped the Flow Run")
        return candidate.relative_to(root).as_posix()

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
        payload = {
            "kind": artifact.kind,
            "path": self._managed_relative(artifact.path, run_root, "artifact"),
            "producer": artifact.producer,
            "qualifiers": json_value(artifact.qualifiers),
        }
        return payload

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
            if (
                asset is None
                or asset.kind != requirement.kind
                or (
                    requirement.identity is not None
                    and asset.identity != requirement.identity
                )
            ):
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
            "reason": outcome.reason,
            "artifacts": {
                role: self._artifact_payload(artifact, run_root)
                for role, artifact in sorted(outcome.artifacts.items())
            },
            "facts": json_value(outcome.facts),
        }
