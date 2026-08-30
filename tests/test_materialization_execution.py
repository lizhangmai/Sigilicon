from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import struct

import pytest

from conftest import StagedAdapterFixture
from sigilicon.flow import (
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_DESIGN_RESULT_KIND,
    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
    PHYSICAL_MATERIALIZATION_PLAN_KIND,
    OA_XSTREAM_MATERIALIZATION_ADAPTER,
    ProducedArtifact,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    register_physical_design_actions,
)
from sigilicon.layout.materialization import (
    MaterializationTarget,
    compile_materialization_plan,
)
from sigilicon.layout.materialization_execution import (
    LayoutArtifactFormat,
    MaterializationCompletion,
    MaterializationExecutionError,
    MaterializationExecutionStatus,
    MaterializationExecutionTarget,
    canonicalize_gdsii_timestamps,
    materialization_receipt_from_json,
    validate_layout_content,
    validate_materialization_receipt,
    validate_materialization_request,
)
from sigilicon.layout.pnr import (
    GridlessRoutingResource,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesign,
    PhysicalDesignJob,
    PhysicalLayer,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    PnrExecutionPolicy,
    PnrRequest,
    PnrStage,
    Rect,
    RoutingDirection,
    run,
)
from sigilicon.workflows.builtin import build_flow_registry
from sigilicon.workflows.physical_design import (
    collect_materialization_execution_result,
    materialization_execution_facts,
    read_materialization_execution_request,
    write_materialization_receipt,
)


_INPUT_ACTION = "contract-fixture.materialization-inputs"
_INPUT_ADAPTER = "contract-materialization-inputs"
_MATERIALIZER = "contract-gds-materializer"


def _job(*, maximum_route_states: int = 200_000) -> PhysicalDesignJob:
    technology = PhysicalTechnology(
        "materialization-execution-gridless",
        1000,
        1,
        layers=(PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 1),
        ),
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "materialization-execution-closed",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(PhysicalNet("signal", (PinReference("source"), PinReference("sink"))),),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(maximum_route_states=maximum_route_states),
    )


def _artifacts(*, maximum_route_states: int = 200_000):
    job = _job(maximum_route_states=maximum_route_states)
    result = run(job)
    plan = compile_materialization_plan(
        job,
        result,
        MaterializationTarget("benchmark", "receipt-bound-layout"),
    )
    return job, result, plan


def _record(record_type: int, data_type: int = 0, data: bytes = b"") -> bytes:
    assert len(data) % 2 == 0
    return struct.pack(">HBB", len(data) + 4, record_type, data_type) + data


def _gds_string(value: str) -> bytes:
    payload = value.encode("ascii")
    return payload if len(payload) % 2 == 0 else payload + b"\0"


def _contract_gds(plan) -> bytes:
    """A structurally real GDSII contract fixture, never signoff evidence."""

    segment = plan.route_segments[0]
    xy = struct.pack(
        ">iiii",
        segment.start.x,
        segment.start.y,
        segment.end.x,
        segment.end.y,
    )
    return b"".join(
        (
            _record(0x00, 0x02, struct.pack(">H", 600)),
            _record(0x01, 0x02, bytes(24)),
            _record(0x02, 0x06, _gds_string("CONTRACT-FIXTURE")),
            _record(0x03, 0x05, bytes(16)),
            _record(0x05, 0x02, bytes(24)),
            _record(0x06, 0x06, _gds_string("RECEIPT-BOUND")),
            _record(0x09),
            _record(0x0D, 0x02, struct.pack(">H", 1)),
            _record(0x0E, 0x02, struct.pack(">H", 0)),
            _record(0x0F, 0x03, struct.pack(">i", segment.width_dbu)),
            _record(0x10, 0x03, xy),
            _record(0x11),
            _record(0x07),
            _record(0x04),
        )
    )


class _InputsAdapter(StagedAdapterFixture):
    def __init__(self, job, result, plan) -> None:
        self.job = job
        self.result = result
        self.plan = plan

    def validate_inputs(self, _context):
        return ()

    def prepare(self, _context):
        pass

    def execute(self, context):
        context.output_path("job", "job.json").write_text(
            self.job.canonical_json(), encoding="utf-8"
        )
        context.output_path("result", "result.json").write_text(
            self.result.canonical_json(), encoding="utf-8"
        )
        context.output_path("plan", "plan.json").write_text(
            self.plan.canonical_json(), encoding="utf-8"
        )
        return AdapterExecution.succeeded()

    def collect_result(self, context, _execution):
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "job",
                    PHYSICAL_DESIGN_JOB_KIND,
                    context.output_path("job", "job.json"),
                ),
                ProducedArtifact(
                    "result",
                    PHYSICAL_DESIGN_RESULT_KIND,
                    context.output_path("result", "result.json"),
                ),
                ProducedArtifact(
                    "plan",
                    PHYSICAL_MATERIALIZATION_PLAN_KIND,
                    context.output_path("plan", "plan.json"),
                ),
            )
        )


class _ContractGdsMaterializer(StagedAdapterFixture):
    """Unregistered test Adapter proving execution semantics, not layout signoff."""

    def validate_inputs(self, context):
        try:
            read_materialization_execution_request(context)
        except Exception as exc:
            return (str(exc),)
        return ()

    def prepare(self, _context):
        pass

    def execute(self, context):
        job, result, plan, target = read_materialization_execution_request(context)
        request = validate_materialization_request(job, result, plan, target)
        raw_status = context.adapter_config.get("outcome", "materialized")
        status = (
            MaterializationExecutionStatus.INVALID_PLAN_IDENTITY
            if not request.valid
            else MaterializationExecutionStatus(raw_status)
        )
        if status is MaterializationExecutionStatus.MATERIALIZED:
            layout_path = context.output_path("layout", "layout.gds")
            layout_path.write_bytes(_contract_gds(plan))
            completion = MaterializationCompletion(
                "sigilicon.test.contract-gds-materializer",
                executed=True,
                completed=True,
                content_validated=True,
                exit_code=0,
            )
        elif status is MaterializationExecutionStatus.EXECUTION_FAILED:
            layout_path = None
            completion = MaterializationCompletion(
                "sigilicon.test.contract-gds-materializer",
                executed=True,
                completed=False,
                content_validated=False,
                exit_code=1,
            )
        else:
            layout_path = None
            completion = MaterializationCompletion(
                "sigilicon.test.contract-gds-materializer",
                executed=False,
                completed=False,
                content_validated=False,
                exit_code=None,
            )
        receipt = write_materialization_receipt(
            context,
            status=status,
            completion=completion,
            layout_path=layout_path,
            message="benchmark contract materialization outcome",
        )
        return AdapterExecution.succeeded(
            details=materialization_execution_facts(receipt)
        )

    def collect_result(self, context, execution):
        return collect_materialization_execution_result(context, execution)


def _engine(job, result, plan) -> FlowEngine:
    registry = FlowRegistry()
    register_physical_design_actions(registry)
    registry.register_action(
        ActionContract(
            _INPUT_ACTION,
            outputs=(
                ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),
                ArtifactPort("result", PHYSICAL_DESIGN_RESULT_KIND),
                ArtifactPort("plan", PHYSICAL_MATERIALIZATION_PLAN_KIND),
            ),
            adapters=(_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(_INPUT_ADAPTER, _InputsAdapter(job, result, plan))
    registry.register_action_adapter(
        PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
        _MATERIALIZER,
        _ContractGdsMaterializer(),
    )
    return FlowEngine(registry)


def _plan(engine: FlowEngine, *, outcome: str = "materialized"):
    spec = FlowSpec(
        owner="benchmark",
        flow_id="materialization-contract",
        nodes=(
            FlowNode("inputs", _INPUT_ACTION),
            FlowNode(
                "materialize",
                PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
                bindings=(
                    ArtifactBinding("job", "inputs", "job"),
                    ArtifactBinding("result", "inputs", "result"),
                    ArtifactBinding("plan", "inputs", "plan"),
                ),
                config={
                    "target": {
                        "owner": "benchmark",
                        "name": "receipt-bound-layout",
                        "format": "gdsii",
                    }
                },
            ),
        ),
        targets=(FlowTarget("materialized", ("materialize",)),),
    )
    profile = ExecutionProfile(
        "benchmark",
        "contract-fixture",
        (
            AdapterSelection(_INPUT_ACTION, _INPUT_ADAPTER),
            AdapterSelection(
                PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
                _MATERIALIZER,
                config={"outcome": outcome},
            ),
        ),
    )
    return engine.plan(spec, "materialized", profile)


def _environment(tmp_path: Path) -> ExecutionEnvironment:
    member_paths = {}
    for role in (
        "oa-target",
        "technology-library",
        "layer-map",
        "master-layouts",
        "via-map",
        "xstream-layer-map",
        "xstream-options",
    ):
        path = tmp_path / f"{role}.fixture"
        path.write_text("contract fixture\n", encoding="utf-8")
        member_paths[role] = path
    return ExecutionEnvironment(
        capabilities={
            "tool.layout-materializer": ResolvedCapability(
                "sigilicon.test.contract-materializer"
            )
        },
        platform_assets=(
            ResolvedPlatformAsset(
                "physical-layout",
                "platform.layout-view-set",
                "benchmark.layout-assets",
                tuple(
                    ResolvedPlatformAssetMember(role, path)
                    for role, path in member_paths.items()
                ),
            ),
        ),
    )


def test_materialization_action_emits_content_bound_receipt(tmp_path: Path) -> None:
    job, result, plan = _artifacts()
    engine = _engine(job, result, plan)
    flow_plan = _plan(engine)

    blocked = engine.preflight(flow_plan, ExecutionEnvironment())
    assert blocked.status == "blocked"
    assert {(check.requirement, check.status) for check in blocked.checks} >= {
        ("tool.layout-materializer", "missing"),
        ("physical-layout", "missing"),
    }

    flow_result = engine.run(
        flow_plan,
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id="1" * 32,
    )

    outcome = flow_result.nodes["materialize"]
    receipt = materialization_receipt_from_json(
        outcome.artifacts["receipt"].path.read_text(encoding="utf-8")
    )
    layout = outcome.artifacts["layout"]
    target = MaterializationExecutionTarget(
        "benchmark", "receipt-bound-layout", LayoutArtifactFormat.GDSII
    )
    validation = validate_materialization_receipt(
        job,
        result,
        plan,
        target,
        receipt,
        layout_path=layout.path,
        run_root=flow_result.run_root,
    )

    assert flow_result.status == "accepted"
    assert outcome.facts == {
        "materialization-status": "materialized",
        "materialized": True,
        "backend-executed": True,
        "backend-completed": True,
    }
    assert receipt.materialized
    assert validation.valid
    assert layout.kind == MATERIALIZED_GDS_KIND
    assert layout.qualifiers["layout-identity"] == receipt.provenance.layout_identity
    assert layout.qualifiers["receipt-identity"] == outcome.artifacts["receipt"].qualifiers[
        "receipt-identity"
    ]
    assert receipt.layout is not None
    forged_run = replace(
        receipt,
        layout=replace(receipt.layout, run_id="f" * 32),
    )
    forged_producer = replace(
        receipt,
        layout=replace(receipt.layout, producer="another-node"),
    )
    for forged in (forged_run, forged_producer):
        forged_validation = validate_materialization_receipt(
            job,
            result,
            plan,
            target,
            forged,
            layout_path=layout.path,
            run_root=flow_result.run_root,
        )
        assert not forged_validation.valid
        assert {item.code for item in forged_validation.issues} == {
            "invalid_layout_content"
        }


@pytest.mark.parametrize(
    "status,executed",
    (
        ("unsupported", False),
        ("backend_unavailable", False),
        ("execution_failed", True),
    ),
)
def test_non_materialized_outcomes_never_publish_layout(
    tmp_path: Path,
    status: str,
    executed: bool,
) -> None:
    job, result, plan = _artifacts()
    engine = _engine(job, result, plan)
    flow_result = engine.run(
        _plan(engine, outcome=status),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id={
            "unsupported": "2" * 32,
            "backend_unavailable": "3" * 32,
            "execution_failed": "4" * 32,
        }[status],
    )

    outcome = flow_result.nodes["materialize"]
    receipt = materialization_receipt_from_json(
        outcome.artifacts["receipt"].path.read_text(encoding="utf-8")
    )
    assert flow_result.status == "accepted"
    assert set(outcome.artifacts) == {"receipt"}
    assert receipt.status.value == status
    assert receipt.completion.executed is executed
    assert receipt.provenance.layout_identity is None


def test_diagnostic_plan_is_rejected_before_backend_execution(tmp_path: Path) -> None:
    job, result, plan = _artifacts(maximum_route_states=1)
    engine = _engine(job, result, plan)
    flow_result = engine.run(
        _plan(engine),
        artifact_root=tmp_path / "artifacts",
        environment=_environment(tmp_path),
        run_id="5" * 32,
    )

    receipt = materialization_receipt_from_json(
        flow_result.nodes["materialize"]
        .artifacts["receipt"]
        .path.read_text(encoding="utf-8")
    )
    assert receipt.status is MaterializationExecutionStatus.INVALID_PLAN_IDENTITY
    assert not receipt.completion.executed
    assert {issue.code for issue in receipt.issues} >= {"plan_not_executable"}
    assert "layout" not in flow_result.nodes["materialize"].artifacts


def test_layout_content_contract_rejects_plan_json_empty_and_arbitrary_bytes() -> None:
    job, result, plan = _artifacts()
    with pytest.raises(MaterializationExecutionError):
        validate_layout_content(b"", LayoutArtifactFormat.GDSII)
    with pytest.raises(MaterializationExecutionError):
        validate_layout_content(plan.canonical_json().encode(), LayoutArtifactFormat.GDSII)
    with pytest.raises(MaterializationExecutionError):
        validate_layout_content(b"not-a-layout", LayoutArtifactFormat.GDSII)


def test_layout_content_contract_rejects_an_empty_gds_structure() -> None:
    empty_structure = b"".join(
        (
            _record(0x00, 0x02, struct.pack(">H", 600)),
            _record(0x01, 0x02, bytes(24)),
            _record(0x02, 0x06, _gds_string("EMPTY")),
            _record(0x03, 0x05, bytes(16)),
            _record(0x05, 0x02, bytes(24)),
            _record(0x06, 0x06, _gds_string("EMPTY")),
            _record(0x07),
            _record(0x04),
        )
    )

    with pytest.raises(MaterializationExecutionError, match="no materialized geometry"):
        validate_layout_content(empty_structure, LayoutArtifactFormat.GDSII)


def test_layout_content_accepts_only_zero_tape_padding_after_endlib() -> None:
    _job, _result, plan = _artifacts()
    payload = _contract_gds(plan)
    padded = payload + bytes(2048 - len(payload))

    validate_layout_content(padded, LayoutArtifactFormat.GDSII)
    canonical = canonicalize_gdsii_timestamps(padded)

    assert len(canonical) == 2048
    assert canonical[len(payload) :] == bytes(2048 - len(payload))
    with pytest.raises(MaterializationExecutionError, match="after ENDLIB"):
        validate_layout_content(payload + b"\0\0BAD!", LayoutArtifactFormat.GDSII)


def test_builtin_registry_exposes_only_the_production_materializer() -> None:
    registry = build_flow_registry()
    contract = registry.action(PHYSICAL_MATERIALIZATION_EXECUTION_ACTION)

    assert contract.adapters == (OA_XSTREAM_MATERIALIZATION_ADAPTER,)
    assert contract.adapter_extensible
    assert not registry.has_adapter(_MATERIALIZER)
    assert registry.has_adapter(OA_XSTREAM_MATERIALIZATION_ADAPTER)
    assert contract.output("layout").kind == MATERIALIZED_GDS_KIND
    assert contract.output("receipt").kind == MATERIALIZATION_RECEIPT_KIND
