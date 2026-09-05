"""Native OA simulation, management, and attestation adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.adapters.cadence._common import (
    AdapterPreparation, Any, CADENCE_SPICEIN_TOOL, CADENCE_SPECTRE_TOOL,
    CADENCE_TEXT_IMPORT_TOOL, CADENCE_VIRTUOSO_TOOL, ContractError,
    ExecutionError, ExecutionIO, Mapping, PreflightCheck, Resources, Step,
    StepResult, _BRIDGE_RESOURCES, _CadenceInputs, Project,
    _OA_CAPABILITIES, _PYTHON, _XRUN, _bridge_check, _capability_checks,
    _executable_check, _oa_resource_identities, _oa_runtime_executables,
    _positive_integer, _prepare_cadence_inputs, _strict_config, _text,
    _validate_oa_plan_sources, canonical_digest, find_oa_assembly,
    json, owned_scratch_directory, process_group_cleanup_uncertainty,
)
from sigilicon.adapters.cadence.oa_library import OALibraryRebuildPlan


@dataclass(frozen=True)
class _NativeOaAction:
    plan: OALibraryRebuildPlan
    inputs: _CadenceInputs

    def __post_init__(self) -> None:
        if not isinstance(self.plan, OALibraryRebuildPlan):
            raise ContractError("native OA action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "native-oa", "inputs": self.inputs.record}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


class NativeOaAdapter:
    """Run one source-owned native Maestro testbench through a bound OA session."""

    name = "cadence.native-oa"
    _fields = frozenset({"owner", "testbench", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        _text(config, "testbench")
        _positive_integer(config, "timeout_seconds")
        compiler = (
            (_XRUN,)
            if isinstance(step.action, _NativeOaAction)
            and _XRUN in step.action.inputs.runtime_identities
            else ()
        )
        return (
            _bridge_check(resources),
            _executable_check(resources, CADENCE_VIRTUOSO_TOOL),
            _executable_check(resources, _PYTHON),
            _executable_check(resources, CADENCE_SPECTRE_TOOL),
            *(_executable_check(resources, name) for name in compiler),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.domain.platform import load_platforms
        from sigilicon.adapters.cadence.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        selected_owner = project.owner(owner)
        manifest = find_oa_assembly(project, selected_owner.root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        platforms = load_platforms(project, resources=resources)
        planning = plan_oa_library_rebuild(
            manifest,
            project=project,
            platform_inventory=platforms,
        )
        testbench = _text(config, "testbench")
        matches = tuple(item for item in planning.testbenches if item.cell == testbench)
        if len(matches) != 1:
            raise ContractError(f"unknown native OA testbench: {testbench}")
        required = _validate_oa_plan_sources(
            project,
            owner,
            planning,
            oa_plan_source_paths(planning),
        )
        prepared = _prepare_cadence_inputs(
            project,
            step,
            resources,
            owner=owner,
            plan_identity=canonical_digest(planning.as_dict()),
            source_records=required,
            resource_identities=_oa_resource_identities(
                project,
                planning,
                required,
                resources,
            ),
            runtime_identities=(
                *_BRIDGE_RESOURCES,
                CADENCE_VIRTUOSO_TOOL,
                _PYTHON,
                CADENCE_SPECTRE_TOOL,
                *((_XRUN,) if matches[0].simulation.simulator == "ams" else ()),
            ),
        )
        return prepared.bind(_NativeOaAction(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.oa_library_execution import (
            build_oa_layout_ir,
        )
        from sigilicon.adapters.cadence.oa_simulation import execute_oa_maestro_testbench

        config = _strict_config(step, self._fields)
        owner = _text(config, "owner")
        testbench = _text(config, "testbench")
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _NativeOaAction):
            raise ExecutionError("native OA Step has no typed action")
        action.inputs.validate(context)
        plan = build_oa_layout_ir(
            action.plan,
            source_paths=action.inputs.source_paths(context),
            workspace=context.workspace("layout-ir", {}),
            python_executable=context.runtime.require_tool(_PYTHON),
        )
        matches = tuple(item for item in plan.testbenches if item.cell == testbench)
        if len(matches) != 1:
            raise ExecutionError(f"prepared native OA testbench is invalid: {testbench}")
        selected = matches[0]
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-maestro-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.workspace(
                    "maestro",
                    {"owner": owner, "testbench": testbench},
                    tool_work_root=scratch.path,
                )
                result = execute_oa_maestro_testbench(
                    plan,
                    selected,
                    get_client(context.runtime),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.register_mutation,
                    resources=context.runtime,
                    record_uncertainty=uncertainty.append,
                    timeout=_positive_integer(config, "timeout_seconds"),
                )
        except Exception:
            published = context.output_artifacts(
                "maestro", "evidence.cadence-maestro"
            )
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    message=" | ".join(uncertainty),
                )
            raise
        published = context.output_artifacts(
            "maestro", "evidence.cadence-maestro"
        )
        if not published:
            raise ExecutionError("native Maestro produced no managed evidence")
        return (
            StepResult.succeeded(artifacts=published)
            if result.passed
            else StepResult(
                "failed",
                published,
                message="native Maestro evidence did not pass",
            )
        )


@dataclass(frozen=True)
class _OaCheckAction:
    plan: OALibraryRebuildPlan
    inputs: _CadenceInputs

    def __post_init__(self) -> None:
        if not isinstance(self.plan, OALibraryRebuildPlan):
            raise ContractError("OA check action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "oa-check", "inputs": self.inputs.record}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


@dataclass(frozen=True)
class _OaRebuildAction:
    plan: OALibraryRebuildPlan
    inputs: _CadenceInputs

    def __post_init__(self) -> None:
        if not isinstance(self.plan, OALibraryRebuildPlan):
            raise ContractError("OA rebuild action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "oa-rebuild", "inputs": self.inputs.record}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


@dataclass(frozen=True)
class _OaAttestAction:
    plan: OALibraryRebuildPlan
    inputs: _CadenceInputs

    def __post_init__(self) -> None:
        if not isinstance(self.plan, OALibraryRebuildPlan):
            raise ContractError("OA attest action requires a typed plan")

    @property
    def record(self) -> dict[str, object]:
        return {"kind": "oa-attest", "inputs": self.inputs.record}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)


type _OaOperationAction = _OaCheckAction | _OaRebuildAction | _OaAttestAction


_OA_FIELDS = frozenset({"owner", "timeout_seconds"})
_OA_ATTEST_FIELDS = frozenset({"owner", "testbench", "timeout_seconds"})


def _oa_config(step: Step, fields: frozenset[str]) -> Mapping[str, Any]:
    config = _strict_config(step, fields)
    _text(config, "owner")
    _positive_integer(config, "timeout_seconds")
    if "testbench" in fields:
        _text(config, "testbench")
    return config


def _oa_preflight(
    step: Step,
    resources: Resources,
    *,
    fields: frozenset[str],
    action_type: type[_OaOperationAction],
) -> tuple[PreflightCheck, ...]:
    _oa_config(step, fields)
    action = step.action
    if action is not None and not isinstance(action, action_type):
        raise ContractError("OA Step has an invalid planned action")
    step.validate_action()
    executables = (
        ()
        if action is None
        else tuple(
            name
            for name in action.inputs.runtime_identities
            if name in {CADENCE_SPICEIN_TOOL, CADENCE_TEXT_IMPORT_TOOL, _PYTHON, _XRUN}
        )
    )
    return (
        _bridge_check(resources),
        *(_executable_check(resources, name) for name in executables),
        *_capability_checks(resources, _OA_CAPABILITIES),
    )


def _prepare_oa(
    project: Project,
    step: Step,
    resources: Resources,
    *,
    fields: frozenset[str],
    operation: str,
):
    from sigilicon.domain.platform import load_platforms
    from sigilicon.adapters.cadence.oa_library import (
        oa_plan_source_paths,
        plan_oa_library_rebuild,
    )

    config = _oa_config(step, fields)
    owner = _text(config, "owner")
    manifest = find_oa_assembly(project, project.owner(owner).root)
    if manifest is None:
        raise ContractError(f"owner {owner!r} has no OA assembly")
    planning = plan_oa_library_rebuild(
        manifest,
        project=project,
        platform_inventory=load_platforms(project, resources=resources),
    )
    if "testbench" in fields:
        testbench = _text(config, "testbench")
        if sum(item.cell == testbench for item in planning.testbenches) != 1:
            raise ContractError(f"unknown native OA testbench: {testbench}")
    required = _validate_oa_plan_sources(
        project,
        owner,
        planning,
        oa_plan_source_paths(planning),
    )
    prepared = _prepare_cadence_inputs(
        project,
        step,
        resources,
        owner=owner,
        plan_identity=canonical_digest(planning.as_dict()),
        source_records=required,
        resource_identities=_oa_resource_identities(
            project,
            planning,
            required,
            resources,
        ),
        runtime_identities=(
            *_BRIDGE_RESOURCES,
            *_oa_runtime_executables(planning, operation),
        ),
    )
    return planning, prepared


def _publish_oa_result(
    context: ExecutionIO,
    operation: str,
    payload: Mapping[str, Any],
) -> StepResult:
    passed = payload.get("passed")
    if type(passed) is not bool:
        raise ExecutionError("OA evidence must contain a boolean 'passed' field")
    context.write_text(
        "oa",
        f"{operation}.json",
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    )
    artifacts = context.output_artifacts("oa", "evidence.cadence-oa", required=True)
    return (
        StepResult.succeeded(artifacts=artifacts)
        if passed
        else StepResult(
            "failed",
            artifacts,
            message=f"OA {operation} did not pass",
        )
    )


class OaCheckAdapter:
    """Validate one planned OA library without mutating its workspace."""

    name = "cadence.oa-check"

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]:
        return _oa_preflight(
            step,
            resources,
            fields=_OA_FIELDS,
            action_type=_OaCheckAction,
        )

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        planning, prepared = _prepare_oa(
            project,
            step,
            resources,
            fields=_OA_FIELDS,
            operation="check",
        )
        return prepared.bind(_OaCheckAction(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        from sigilicon.adapters.cadence.oa_check import check_oa_library
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.oa_library_execution import (
            build_oa_layout_ir,
        )
        from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation

        config = _oa_config(context.step, _OA_FIELDS)
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _OaCheckAction):
            raise ExecutionError("OA check Step has no typed action")
        action.inputs.validate(context)
        planning = build_oa_layout_ir(
            action.plan,
            source_paths=action.inputs.source_paths(context),
            workspace=context.workspace("layout-ir", {}),
            python_executable=context.runtime.require_tool(_PYTHON),
        )
        client = get_client(context.runtime)
        with workspace_operation(
            client,
            action.inputs.workspace_root,
            "check-oa-library",
            policy=OperationPolicy.READ_ONLY,
            acquire_flow_lock=False,
            record_incident=False,
            operation_id=context.operation_id,
        ) as operation:
            context.register_mutation(operation)
            payload = check_oa_library(
                planning,
                client=client,
                timeout=_positive_integer(config, "timeout_seconds"),
                operation=operation,
            )
        return _publish_oa_result(context, "check", payload)


class OaRebuildAdapter:
    """Rebuild one planned OA assembly through a bound mutation lease."""

    name = "cadence.oa-rebuild"

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]:
        return _oa_preflight(
            step,
            resources,
            fields=_OA_FIELDS,
            action_type=_OaRebuildAction,
        )

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        planning, prepared = _prepare_oa(
            project,
            step,
            resources,
            fields=_OA_FIELDS,
            operation="rebuild",
        )
        return prepared.bind(_OaRebuildAction(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.oa_library_execution import (
            build_oa_layout_ir,
            rebuild_oa_library,
        )

        config = _oa_config(context.step, _OA_FIELDS)
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _OaRebuildAction):
            raise ExecutionError("OA rebuild Step has no typed action")
        action.inputs.validate(context)
        planning = build_oa_layout_ir(
            action.plan,
            source_paths=action.inputs.source_paths(context),
            workspace=context.workspace("layout-ir", {}),
            python_executable=context.runtime.require_tool(_PYTHON),
        )
        payload = rebuild_oa_library(
            planning,
            get_client(context.runtime),
            source_paths=action.inputs.source_paths(context),
            resource_paths=action.inputs.resource_paths(context),
            resources=context.runtime,
            artifacts=context.workspace("oa", {}).scoped("layouts"),
            timeout=_positive_integer(config, "timeout_seconds"),
            operation_id=context.operation_id,
            bind_operation=context.register_mutation,
        )
        return _publish_oa_result(context, "rebuild", payload)


class OaAttestAdapter:
    """Attest one native OA testbench against its planned assembly."""

    name = "cadence.oa-attest"

    def preflight(
        self,
        step: Step,
        resources: Resources,
    ) -> tuple[PreflightCheck, ...]:
        return _oa_preflight(
            step,
            resources,
            fields=_OA_ATTEST_FIELDS,
            action_type=_OaAttestAction,
        )

    def prepare(
        self,
        project: Project,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        planning, prepared = _prepare_oa(
            project,
            step,
            resources,
            fields=_OA_ATTEST_FIELDS,
            operation="attest",
        )
        return prepared.bind(_OaAttestAction(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.oa_library_execution import (
            attest_oa_testbench,
        )

        config = _oa_config(context.step, _OA_ATTEST_FIELDS)
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, _OaAttestAction):
            raise ExecutionError("OA attest Step has no typed action")
        action.inputs.validate(context)
        testbench = _text(config, "testbench")
        matches = tuple(
            item for item in action.plan.testbenches if item.cell == testbench
        )
        if len(matches) != 1:
            raise ExecutionError(f"prepared OA testbench is invalid: {testbench}")
        payload = attest_oa_testbench(
            action.plan,
            matches[0],
            get_client(context.runtime),
            timeout=_positive_integer(config, "timeout_seconds"),
            operation_id=context.operation_id,
            bind_operation=context.register_mutation,
        )
        return _publish_oa_result(context, "attest", payload)
