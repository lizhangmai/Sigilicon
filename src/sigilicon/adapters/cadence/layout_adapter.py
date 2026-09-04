"""Custom-layout generation and verification adapters."""

from __future__ import annotations

from sigilicon.adapters.cadence._common import (
    AdapterPreparation, Any, ContractError, ExecutionError, ExecutionIO, Mapping,
    Path, PreflightCheck, Resources, Step, StepResult, _BRIDGE_RESOURCES,
    _CALIBRE, _CadenceDomainAdapter, _CadencePlanningProject, _OA_CAPABILITIES,
    _PYTHON, _XSTREAM, _bridge_check, _capability_checks, _executable_check,
    _positive_integer, _relative, _strict_config, _text, canonical_digest, json,
    owned_scratch_directory, process_group_cleanup_uncertainty,
)

class LayoutAdapter(_CadenceDomainAdapter):
    """Generate one source-authored layout through a bound OA mutation lease."""

    name = "cadence.layout"
    _fields = frozenset({"owner", "spec", "timeout_seconds"})

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError("layout spec must be inside the operation source closure")
        _positive_integer(config, "timeout_seconds")
        return (
            _bridge_check(resources),
            _executable_check(resources, _PYTHON),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def prepare(
        self,
        project: _CadencePlanningProject,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.domain.platform import (
            load_platforms,
            platform_resource_identities,
        )
        from sigilicon.adapters.cadence.layout_generation import plan_layout_spec

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        platforms = load_platforms(project, resources=resources)
        planning = plan_layout_spec(
            spec,
            project=project,
            platform=platforms,
        )
        return self._prepare_domain_step(
            project,
            step,
            resources,
            owner=owner,
            action_kind="layout",
            plan=planning,
            plan_identity=canonical_digest(
                {
                    "library": planning.spec.library,
                    "cell": planning.spec.cell,
                    "view": planning.spec.view,
                    "generator": planning.spec.generator,
                    "stage": planning.spec.stage,
                }
            ),
            source_records=planning.source_records,
            resource_identities=platform_resource_identities(planning.spec.pdk),
            runtime_identities=(*_BRIDGE_RESOURCES, _PYTHON),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.layout_generation import build_managed_layout_ir

        prepared = self._domain_action(context, "layout")
        planning = build_managed_layout_ir(
            prepared.plan,
            source_paths=prepared.source_paths(context),
            workspace=context.workspace("layout-ir", {}),
            python_executable=context.runtime.require_tool(_PYTHON),
        )
        return self._execute(context, planning)

    def _execute(self, context: ExecutionIO, planning: Any) -> StepResult:
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.layout_generation import generate_layout

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-layout-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.workspace(
                    "layout",
                    {"owner": owner, "spec": str(config["spec"])},
                    tool_work_root=scratch.path,
                )
                result = generate_layout(
                    planning,
                    get_client(context.runtime),
                    artifacts=artifacts,
                    operation_id=context.operation_id,
                    bind_operation=context.register_mutation,
                    record_uncertainty=uncertainty.append,
                    timeout=_positive_integer(config, "timeout_seconds"),
                )
        except Exception:
            published = context.output_artifacts(
                "layout", "evidence.cadence-layout"
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
            "layout", "evidence.cadence-layout"
        )
        if not published:
            raise ExecutionError("layout generation produced no managed evidence")
        return StepResult.succeeded(
            artifacts=published,
            facts={"instance_count": result.instance_count},
        )


class LayoutVerificationAdapter(_CadenceDomainAdapter):
    """Verify one existing routed OA layout with XStream and Calibre."""

    name = "cadence.layout-verify"
    _fields = frozenset(
        {
            "owner",
            "spec",
            "check",
            "xstream_timeout_seconds",
            "calibre_timeout_seconds",
        }
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        config = _strict_config(step, self._fields)
        if step.evidence is None:
            raise ContractError("layout verification requires an evidence envelope")
        _text(config, "owner")
        spec = _relative(_text(config, "spec"), "layout spec")
        if spec not in step.sources:
            raise ContractError(
                "layout verification spec must be inside the source closure"
            )
        if _text(config, "check") not in {"drc", "lvs"}:
            raise ContractError("layout verification check must be drc or lvs")
        _positive_integer(config, "xstream_timeout_seconds")
        _positive_integer(config, "calibre_timeout_seconds")
        return (
            _bridge_check(resources),
            _executable_check(resources, _PYTHON),
            _executable_check(resources, _XSTREAM),
            _executable_check(resources, _CALIBRE),
            *_capability_checks(resources, _OA_CAPABILITIES),
        )

    def prepare(
        self,
        project: _CadencePlanningProject,
        step: Step,
        resources: Resources,
    ) -> AdapterPreparation:
        from sigilicon.domain.platform import (
            load_platforms,
            platform_resource_identities,
        )
        from sigilicon.adapters.cadence.layout_generation import plan_layout_spec

        initial = step
        config = _strict_config(initial, self._fields)
        owner = _text(config, "owner")
        spec = project.owner(owner).root / _relative(
            _text(config, "spec"), "layout spec"
        )
        platforms = load_platforms(project, resources=resources)
        planning = plan_layout_spec(
            spec,
            project=project,
            platform=platforms,
        )
        if planning.spec.layout_pdk is None:
            raise ContractError("layout verification requires a layout PDK")
        deck = (
            planning.spec.layout_pdk.drc_deck.require_path()
            if _text(config, "check") == "drc"
            else planning.spec.layout_pdk.lvs_deck.require_path()
        )
        check = _text(config, "check")
        return self._prepare_domain_step(
            project,
            step,
            resources,
            owner=owner,
            action_kind="layout-verify",
            plan=planning,
            plan_identity=canonical_digest(
                {
                    "library": planning.spec.library,
                    "cell": planning.spec.cell,
                    "view": planning.spec.view,
                    "generator": planning.spec.generator,
                    "stage": planning.spec.stage,
                    "check": check,
                }
            ),
            source_records=planning.source_records,
            resource_identities=platform_resource_identities(planning.spec.pdk),
            extra_resources=(planning.spec.layout_pdk.layermap.require_path(), deck),
            runtime_identities=(*_BRIDGE_RESOURCES, _PYTHON, _XSTREAM, _CALIBRE),
        )

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        from sigilicon.adapters.cadence.layout_generation import build_managed_layout_ir

        config = _strict_config(step, self._fields)
        check = _text(config, "check")
        prepared = self._domain_action(context, "layout-verify")
        planning = build_managed_layout_ir(
            prepared.plan,
            source_paths=prepared.source_paths(context),
            workspace=context.workspace("layout-ir", {}),
            python_executable=context.runtime.require_tool(_PYTHON),
        )
        return self._execute(
            context,
            planning,
            prepared.resource_text(context),
        )

    def _execute(
        self,
        context: ExecutionIO,
        planning: Any,
        external_sources: Mapping[Path, str],
    ) -> StepResult:
        from sigilicon.adapters.cadence.oa_client import get_client
        from sigilicon.adapters.cadence.layout_verification import run_layout_verification

        config = _strict_config(context.step, self._fields)
        owner = _text(config, "owner")
        uncertainty: list[str] = []
        try:
            with owned_scratch_directory(
                prefix=f"sigilicon-physical-{context.run_id}-",
                retain_on_error=lambda exc: bool(uncertainty)
                or process_group_cleanup_uncertainty(exc) is not None,
            ) as scratch:
                artifacts = context.workspace(
                    "verification",
                    {
                        "owner": owner,
                        "spec": str(config["spec"]),
                        "check": str(config["check"]),
                    },
                    tool_work_root=scratch.path,
                )
                result = run_layout_verification(
                    planning,
                    get_client(context.runtime),
                    check=_text(config, "check"),
                    artifacts=artifacts,
                    resources=context.runtime,
                    external_sources=external_sources,
                    operation_id=context.operation_id,
                    bind_operation=context.register_mutation,
                    record_uncertainty=uncertainty.append,
                    xstream_timeout=_positive_integer(
                        config, "xstream_timeout_seconds"
                    ),
                    calibre_timeout=_positive_integer(
                        config, "calibre_timeout_seconds"
                    ),
                )
        except Exception:
            published = context.output_artifacts(
                "verification", "evidence.physical-verification"
            )
            if uncertainty:
                return StepResult(
                    "uncertain",
                    published,
                    {"workspace_uncertainty": tuple(uncertainty)},
                    " | ".join(uncertainty),
                )
            raise
        envelope = context.step.evidence
        if envelope is None:
            raise ExecutionError("layout verification lost its evidence envelope")
        context.write_text(
            "verification",
            "flow-evidence.json",
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "physical-verification-evidence",
                    "plan_identity": context.plan_identity,
                    "platform": planning.spec.pdk.key,
                    "check": str(config["check"]),
                    "evidence_role": envelope.role,
                    "evidence_level": envelope.level,
                    "evidence_scope": envelope.scope,
                    "physical_verification": json.loads(
                        result.evidence.canonical_json()
                    ),
                    "product_qualification_conclusion": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        published = context.output_artifacts(
            "verification", "evidence.physical-verification"
        )
        if not published:
            raise ExecutionError("layout verification produced no managed evidence")
        facts = {
            "passed": result.passed,
            "check": str(config["check"]),
            "status": result.evidence.status.value,
            "evidence_role": envelope.role,
            "evidence_level": envelope.level,
            "evidence_scope": envelope.scope,
            "product_qualification_conclusion": False,
        }
        return (
            StepResult.succeeded(artifacts=published, facts=facts)
            if result.passed
            else StepResult(
                "failed",
                published,
                facts,
                f"Calibre {str(config['check']).upper()} did not prove clean",
            )
        )
