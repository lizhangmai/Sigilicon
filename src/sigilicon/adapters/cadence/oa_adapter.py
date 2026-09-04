"""Native OA simulation, management, and attestation adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.adapters.cadence._common import (
    AdapterPreparation, Any, Artifact, CADENCE_SPICEIN_TOOL,
    CADENCE_TEXT_IMPORT_TOOL, CADENCE_VIRTUOSO_TOOL, ContractError,
    ExecutionError, ExecutionIO, Mapping, PreflightCheck, Resources, Step,
    StepResult, _BRIDGE_RESOURCES, _CadenceInputs, _CadencePlanningProject,
    _OA_CAPABILITIES, _PYTHON, _bridge_check, _capability_checks,
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
        if "configs/oa.toml" not in step.sources:
            raise ContractError("native OA step must close over configs/oa.toml")
        return (
            _bridge_check(resources),
            _executable_check(resources, CADENCE_VIRTUOSO_TOOL),
            _executable_check(resources, _PYTHON),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def prepare(
        self,
        project: _CadencePlanningProject,
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
            ),
        )
        return prepared.bind(_NativeOaAction(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.oa_library import build_oa_layout_ir
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
                    {"workspace_uncertainty": tuple(uncertainty)},
                    " | ".join(uncertainty),
                )
            raise
        published = context.output_artifacts(
            "maestro", "evidence.cadence-maestro"
        )
        if not published:
            raise ExecutionError("native Maestro produced no managed evidence")
        return (
            StepResult.succeeded(
                artifacts=published,
                facts={"passed": result.passed, "evidence_status": result.evidence.status},
            )
            if result.passed
            else StepResult(
                "failed",
                published,
                {"passed": False, "evidence_status": result.evidence.status},
                "native Maestro evidence did not pass",
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


class _OaAdapter:
    """Common planning and publication for one fixed native-OA operation."""

    _base_fields = frozenset({"owner", "timeout_seconds"})
    name: str
    operation: str
    requires_testbench = False
    materializes_layout_ir = False

    def _config(self, step: Step) -> Mapping[str, Any]:
        fields = self._base_fields | (
            {"testbench"} if self.requires_testbench else set()
        )
        config = _strict_config(step, frozenset(fields))
        _text(config, "owner")
        _positive_integer(config, "timeout_seconds")
        if self.requires_testbench:
            _text(config, "testbench")
        if "configs/oa.toml" not in step.sources:
            raise ContractError("OA management step must close over configs/oa.toml")
        return config

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        self._config(step)
        action = step.action
        expected = {
            "check": _OaCheckAction,
            "rebuild": _OaRebuildAction,
            "attest": _OaAttestAction,
        }[self.operation]
        if action is not None and not isinstance(action, expected):
            raise ContractError("OA Step has an invalid planned action")
        step.validate_action()
        runtime_executables: tuple[str, ...] = ()
        if action is not None:
            runtime_executables = tuple(
                item
                for item in action.inputs.runtime_identities
                if item in {
                    CADENCE_SPICEIN_TOOL,
                    CADENCE_TEXT_IMPORT_TOOL,
                    _PYTHON,
                }
            )
        return (
            _bridge_check(resources),
            *(
                _executable_check(resources, name)
                for name in runtime_executables
            ),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def _execute_operation(
        self,
        context: ExecutionIO,
        *,
        planning: OALibraryRebuildPlan,
        prepared: _OaOperationAction,
        selected: Any,
        client: Any,
        timeout: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def prepare(
        self,
        project: _CadencePlanningProject,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.domain.platform import load_platforms
        from sigilicon.adapters.cadence.oa_library import (
            oa_plan_source_paths,
            plan_oa_library_rebuild,
        )

        initial = step
        config = self._config(initial)
        owner = _text(config, "owner")
        manifest = find_oa_assembly(project, project.owner(owner).root)
        if manifest is None:
            raise ContractError(f"owner {owner!r} has no OA assembly")
        platforms = load_platforms(project, resources=resources)
        planning = plan_oa_library_rebuild(
            manifest,
            project=project,
            platform_inventory=platforms,
        )
        selected = None
        if self.requires_testbench:
            testbench = _text(config, "testbench")
            matches = tuple(
                item for item in planning.testbenches if item.cell == testbench
            )
            if len(matches) != 1:
                raise ContractError(f"unknown native OA testbench: {testbench}")
            selected = matches[0]
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
                *_oa_runtime_executables(planning, self.operation),
            ),
        )
        action_type = {
            "check": _OaCheckAction,
            "rebuild": _OaRebuildAction,
            "attest": _OaAttestAction,
        }[self.operation]
        return prepared.bind(action_type(planning, prepared.inputs))

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.oa_library import (
            build_oa_layout_ir,
        )

        config = self._config(step)
        owner = _text(config, "owner")
        context.step.validate_action()
        prepared = context.step.action
        expected = {
            "check": _OaCheckAction,
            "rebuild": _OaRebuildAction,
            "attest": _OaAttestAction,
        }[self.operation]
        if not isinstance(prepared, expected):
            raise ExecutionError(f"OA {self.operation} Step has no typed action")
        prepared.inputs.validate(context)
        planning = prepared.plan
        if self.materializes_layout_ir:
            planning = build_oa_layout_ir(
                planning,
                source_paths=prepared.inputs.source_paths(context),
                workspace=context.workspace("layout-ir", {}),
                python_executable=context.runtime.require_tool(_PYTHON),
            )
        selected = None
        if self.requires_testbench:
            testbench = _text(config, "testbench")
            matches = tuple(item for item in planning.testbenches if item.cell == testbench)
            if len(matches) != 1:
                raise ExecutionError(f"prepared OA testbench is invalid: {testbench}")
            selected = matches[0]
        timeout = _positive_integer(config, "timeout_seconds")
        client = get_client(context.runtime)
        payload = self._execute_operation(
            context,
            planning=planning,
            prepared=prepared,
            selected=selected,
            client=client,
            timeout=timeout,
        )
        passed = payload.get("passed")
        if type(passed) is not bool:
            raise ExecutionError("OA evidence must contain a boolean 'passed' field")
        output = context.write_text(
            "oa",
            f"{self.operation}.json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
        artifacts = (Artifact("oa", "evidence.cadence-oa", output),)
        facts = {"passed": passed, "operation": self.operation}
        return (
            StepResult.succeeded(artifacts=artifacts, facts=facts)
            if passed
            else StepResult(
                "failed",
                artifacts,
                facts,
                f"OA {self.operation} did not pass",
            )
        )


class OaCheckAdapter(_OaAdapter):
    """Validate one planned OA library without mutating its workspace."""

    name = "cadence.oa-check"
    operation = "check"
    materializes_layout_ir = True

    def _execute_operation(
        self,
        context: ExecutionIO,
        *,
        planning: OALibraryRebuildPlan,
        prepared: _OaOperationAction,
        selected: Any,
        client: Any,
        timeout: int,
    ) -> Mapping[str, Any]:
        from sigilicon.adapters.cadence.oa_check import check_oa_library
        from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation

        with workspace_operation(
            client,
            prepared.inputs.workspace_root,
            "check-oa-library",
            policy=OperationPolicy.READ_ONLY,
            acquire_flow_lock=False,
            record_incident=False,
            operation_id=context.operation_id,
        ) as operation:
            context.register_mutation(operation)
            return check_oa_library(
                planning,
                client=client,
                timeout=timeout,
                operation=operation,
            )


class OaRebuildAdapter(_OaAdapter):
    """Rebuild one planned OA assembly through a bound mutation lease."""

    name = "cadence.oa-rebuild"
    operation = "rebuild"
    materializes_layout_ir = True

    def _execute_operation(
        self,
        context: ExecutionIO,
        *,
        planning: OALibraryRebuildPlan,
        prepared: _OaOperationAction,
        selected: Any,
        client: Any,
        timeout: int,
    ) -> Mapping[str, Any]:
        from sigilicon.adapters.cadence.oa_library import rebuild_oa_library

        return rebuild_oa_library(
            planning,
            client,
            source_paths=prepared.inputs.source_paths(context),
            resource_paths=prepared.inputs.resource_paths(context),
            resources=context.runtime,
            timeout=timeout,
            operation_id=context.operation_id,
            bind_operation=context.register_mutation,
        )


class OaAttestAdapter(_OaAdapter):
    """Attest one native OA testbench against its planned assembly."""

    name = "cadence.oa-attest"
    operation = "attest"
    requires_testbench = True

    def _execute_operation(
        self,
        context: ExecutionIO,
        *,
        planning: OALibraryRebuildPlan,
        prepared: _OaOperationAction,
        selected: Any,
        client: Any,
        timeout: int,
    ) -> Mapping[str, Any]:
        from sigilicon.adapters.cadence.oa_library import attest_oa_testbench

        if selected is None:
            raise ExecutionError("OA attest preparation lost its testbench")
        return attest_oa_testbench(
            planning,
            selected,
            client,
            timeout=timeout,
            operation_id=context.operation_id,
            bind_operation=context.register_mutation,
        )
