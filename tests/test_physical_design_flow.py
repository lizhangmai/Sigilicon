from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from conftest import StagedAdapterFixture
from sigilicon.flow import (
    ActionContext,
    ActionBinding,
    ActionContract,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FactSet,
    FactSource,
    FlowEngine,
    FlowNode,
    FlowSpec,
    FlowTarget,
    PolicyCheck,
    PolicySpec,
    ProducedArtifact,
)
from sigilicon.flow.physical_design import (
    MATERIALIZATION_PLAN_ADAPTER,
    PHYSICAL_MATERIALIZATION_ACTION,
    PHYSICAL_DESIGN_ACTION,
    PHYSICAL_DESIGN_JOB_KIND,
)
from sigilicon.experimental.reference_pnr.flow import (
    REFERENCE_PHYSICAL_DESIGN_ACTION,
    REFERENCE_PNR_ADAPTER,
)
from sigilicon.layout.materialization import (
    MaterializationDecision,
    materialization_plan_from_json,
)
from sigilicon.canonical import CanonicalSerializationError
from sigilicon.layout.physical_design import (
    Axis,
    GridlessRoutingResource,
    LayerKind,
    LayerShape,
    MinimumSpacingRule,
    MinimumWidthRule,
    PhysicalDesign,
    PhysicalLayer,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Placement,
    PhysicalDesignRequest,
    PhysicalDesignStage,
    Point,
    Rect,
    ResultStatus,
    RoutingBlockage,
    RoutingDirection,
    RoutingTrackPattern,
    PhysicalDesignJob,
    PhysicalDesignResult,
)
from sigilicon.experimental.reference_pnr import (
    PlacementRoutingTerminationReason,
    ReferencePnrExecutionPolicy,
    ReferencePnrJob,
    RoutingTerminationReason,
    run,
)
from sigilicon.experimental.reference_pnr.serialization import (
    physical_closure_evidence_id,
    placement_routing_closure_evidence_from_json,
    reference_pnr_result_from_json,
)
from sigilicon.layout.physical_design_serialization import (
    physical_design_job_from_json,
    physical_design_job_id,
    physical_design_result_from_json,
    physical_design_result_id,
)
from sigilicon.experimental.registry import build_experimental_flow_registry


def _routing_technology() -> PhysicalTechnology:
    return PhysicalTechnology(
        "flow-benchmark-gridless",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(
            PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),
        ),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 1),
        ),
    )


def _capacity_job(*, maximum_iterations: int = 8) -> ReferencePnrJob:
    return ReferencePnrJob(
        _routing_technology(),
        PhysicalDesign(
            "flow-capacity-negotiation",
            Rect(0, 0, 8, 10),
            (),
            (),
            ports=(
                PhysicalPort("a-source", (PinAccess("route", Rect(1, 0, 3, 2)),)),
                PhysicalPort("a-sink", (PinAccess("route", Rect(3, 0, 5, 2)),)),
                PhysicalPort("b-source", (PinAccess("route", Rect(1, 3, 3, 5)),)),
                PhysicalPort("b-sink", (PinAccess("route", Rect(3, 3, 5, 5)),)),
            ),
            nets=(
                PhysicalNet(
                    "a-direct",
                    (PinReference("a-source"), PinReference("a-sink")),
                ),
                PhysicalNet(
                    "b-negotiated",
                    (PinReference("b-source"), PinReference("b-sink")),
                ),
            ),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
        execution_policy=ReferencePnrExecutionPolicy(
            maximum_routing_iterations=maximum_iterations,
            routing_congestion_bins_x=1,
            routing_congestion_bins_y=2,
        ),
    )


def _fixed_blockage_job() -> ReferencePnrJob:
    technology = PhysicalTechnology(
        "flow-benchmark-fixed-blocker",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(
            PhysicalLayer(
                "route",
                LayerKind.ROUTING,
                RoutingDirection.HORIZONTAL,
            ),
        ),
        routing_resources=(
            RoutingTrackPattern("only-track", "route", Axis.Y, 2, 4, 1),
        ),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )
    return ReferencePnrJob(
        technology,
        PhysicalDesign(
            "flow-fixed-blocker",
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
            routing_blockages=(
                RoutingBlockage(
                    "fixed-channel-blocker",
                    2,
                    2,
                    (LayerShape("route", Rect(0, 0, 2, 2)),),
                    Placement(Point(7, 0)),
                ),
            ),
        ),
        request=PhysicalDesignRequest(stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)),
    )


def _state_budget_job() -> ReferencePnrJob:
    job = _capacity_job()
    return replace(
        job,
        execution_policy=replace(job.execution_policy, maximum_route_states=1),
    )


class _BenchmarkJobAdapter(StagedAdapterFixture):
    def __init__(self, jobs: dict[str, PhysicalDesignJob]) -> None:
        self._jobs = jobs

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return () if context.action_config.get("job") in self._jobs else ("unknown job",)

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        job = self._jobs[str(context.action_config["job"])]
        stable_job = PhysicalDesignJob(
            technology=job.technology,
            design=job.design,
            constraints=job.constraints,
            request=job.request,
            routing_constraints=job.routing_constraints,
        )
        context.output_path("job", "physical-design-job.json").write_text(
            stable_job.canonical_json(),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            facts=FactSet.empty(
                context.action.fact_schema,
                source=FactSource(context.action.kind, context.node_id),
            ),
            artifacts=(
                ProducedArtifact(
                    "job",
                    PHYSICAL_DESIGN_JOB_KIND,
                    context.output_path("job", "physical-design-job.json"),
                ),
            ),
        )


def _run_flow(
    tmp_path: Path,
    job_name: str,
    job: PhysicalDesignJob,
    *,
    run_id: str,
):
    registry = build_experimental_flow_registry()
    registry.register_action(
        ActionContract(
            "benchmark.physical-job",
            outputs=(ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),),
            adapters=("benchmark-job",),
        )
    )
    registry.register_adapter("benchmark-job", _BenchmarkJobAdapter({job_name: job}))
    spec = FlowSpec(
        owner="benchmark",
        flow_id="physical-design-flow",
        recipe_id="physical-design-flow-recipe",
        nodes=(
            FlowNode(
                "job",
                "benchmark.physical-job",
                config={"job": job_name},
            ),
            FlowNode(
                "solve",
                REFERENCE_PHYSICAL_DESIGN_ACTION,
                config={
                    "reference-policy": (
                        None
                        if not isinstance(job, ReferencePnrJob)
                        else {
                            name: getattr(job.execution_policy, name)
                            for name in (
                                "maximum_search_states",
                                "maximum_route_states",
                                "maximum_routing_iterations",
                                "maximum_placement_repair_states",
                                "maximum_placement_repair_iterations",
                                "routing_congestion_bins_x",
                                "routing_congestion_bins_y",
                            )
                        }
                    )
                },
                bindings=(ArtifactBinding("job", "job", "job"),),
                policy="require-closure",
            ),
            FlowNode(
                "materialize",
                PHYSICAL_MATERIALIZATION_ACTION,
                config={
                    "target": {
                        "owner": "benchmark",
                        "name": "layout-candidate",
                    }
                },
                bindings=(
                    ArtifactBinding("job", "job", "job"),
                    ArtifactBinding(
                        "result",
                        "solve",
                        "result",
                        requires="valid",
                    ),
                ),
                policy="require-executable",
            ),
        ),
        targets=(FlowTarget("closure", ("materialize",)),),
        policies=(
            PolicySpec(
                "require-closure",
                (
                    PolicyCheck(
                        "closed",
                        "physical-design-closed",
                        "equals",
                        True,
                    ),
                ),
            ),
            PolicySpec(
                "require-executable",
                (
                    PolicyCheck(
                        "executable",
                        "materialization-executable",
                        "equals",
                        True,
                    ),
                ),
            ),
        ),
        action_bindings=(
            ActionBinding("benchmark.physical-job", "benchmark-job"),
            ActionBinding(REFERENCE_PHYSICAL_DESIGN_ACTION, REFERENCE_PNR_ADAPTER),
            ActionBinding(
                PHYSICAL_MATERIALIZATION_ACTION,
                MATERIALIZATION_PLAN_ADAPTER,
            ),
        ),
    )
    engine = FlowEngine(registry)
    plan = engine.plan(spec, "closure")
    return engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id=run_id,
    )


def test_physical_design_serialization_is_reversible_and_strict() -> None:
    job = _capacity_job()
    result = run(job)
    stable_job = PhysicalDesignJob(
        technology=job.technology,
        design=job.design,
        constraints=job.constraints,
        request=job.request,
        routing_constraints=job.routing_constraints,
    )

    loaded_job = physical_design_job_from_json(stable_job.canonical_json())
    loaded_result = reference_pnr_result_from_json(result.canonical_json())

    assert loaded_job == stable_job
    assert loaded_job.canonical_json() == stable_job.canonical_json()
    assert loaded_result == result
    assert loaded_result.canonical_json() == result.canonical_json()

    raw = json.loads(stable_job.canonical_json())
    raw["unknown"] = True
    with pytest.raises(CanonicalSerializationError, match="unknown=.*unknown"):
        physical_design_job_from_json(json.dumps(raw))

    raw = json.loads(stable_job.canonical_json())
    raw["request"]["stages"][0] = "unknown-stage"
    with pytest.raises(CanonicalSerializationError, match="unknown PhysicalDesignStage"):
        physical_design_job_from_json(json.dumps(raw))

    raw = json.loads(stable_job.canonical_json())
    raw["design"]["die"] = []
    with pytest.raises(CanonicalSerializationError, match="must be a Rect object"):
        physical_design_job_from_json(json.dumps(raw))


def test_physical_design_job_identity_binds_complete_stable_intent() -> None:
    reference = _capacity_job()
    job = PhysicalDesignJob(
        technology=reference.technology,
        design=reference.design,
        constraints=reference.constraints,
        request=reference.request,
        routing_constraints=reference.routing_constraints,
    )
    changed_request = replace(
        job,
        request=replace(job.request, minimum_instance_spacing_dbu=1),
    )

    identity = physical_design_job_id(job)
    assert identity != physical_design_job_id(changed_request)
    assert identity.startswith(
        "physical-design-job:flow-benchmark-gridless:flow-capacity-negotiation:sha256:"
    )
    assert len(identity.rsplit(":sha256:", 1)[1]) == 64


@pytest.mark.parametrize(
    ("name", "job", "status", "routing_termination", "state_budget", "iteration_budget"),
    (
        (
            "closed",
            _capacity_job(),
            ResultStatus.SUCCEEDED,
            RoutingTerminationReason.CLOSED,
            False,
            False,
        ),
        (
            "fixed-blocker",
            _fixed_blockage_job(),
            ResultStatus.FAILED,
            RoutingTerminationReason.INFEASIBLE,
            False,
            False,
        ),
        (
            "state-budget",
            _state_budget_job(),
            ResultStatus.EXHAUSTED,
            RoutingTerminationReason.STATE_BUDGET,
            True,
            False,
        ),
        (
            "iteration-budget",
            _capacity_job(maximum_iterations=1),
            ResultStatus.EXHAUSTED,
            RoutingTerminationReason.ITERATION_BUDGET,
            False,
            True,
        ),
    ),
)
def test_reference_pnr_runs_through_public_flow_engine(
    tmp_path: Path,
    name: str,
    job: PhysicalDesignJob,
    status: ResultStatus,
    routing_termination: RoutingTerminationReason,
    state_budget: bool,
    iteration_budget: bool,
) -> None:
    flow = _run_flow(tmp_path / name, name, job, run_id="1" * 32)
    outcome = flow.nodes["solve"]
    result_artifact = outcome.artifacts["result"]
    result = physical_design_result_from_json(
        result_artifact.path.read_text(encoding="utf-8")
    )
    evidence_artifact = outcome.artifacts["closure-evidence"]
    evidence = placement_routing_closure_evidence_from_json(
        evidence_artifact.path.read_text(encoding="utf-8")
    )
    materialization = flow.nodes["materialize"]
    materialization_plan = materialization_plan_from_json(
        materialization.artifacts["plan"].path.read_text(encoding="utf-8")
    )

    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "valid"
    assert result.status is status
    assert result.closed is (status is ResultStatus.SUCCEEDED)
    assert evidence.routing_termination is routing_termination
    assert outcome.facts["state-budget-exhausted"] is state_budget
    assert outcome.facts["iteration-budget-exhausted"] is iteration_budget
    assert result_artifact.qualifiers["result-identity"] == physical_design_result_id(result)
    assert evidence_artifact.qualifiers["closure-identity"] == physical_closure_evidence_id(evidence)
    assert materialization.execution_status == "succeeded"
    assert materialization.result_status == "valid"
    assert materialization_plan.executable is (status is ResultStatus.SUCCEEDED)
    assert materialization_plan.acceptance.decision is (
        MaterializationDecision.EXECUTABLE
        if status is ResultStatus.SUCCEEDED
        else MaterializationDecision.DIAGNOSTIC
        if status is ResultStatus.EXHAUSTED
        else MaterializationDecision.REJECTED
    )
    assert flow.status == ("accepted" if status is ResultStatus.SUCCEEDED else "failed")
    if name == "fixed-blocker":
        assert evidence.termination is PlacementRoutingTerminationReason.NO_LEGAL_REPAIR


def test_reference_pnr_artifact_identity_is_deterministic_across_runs(
    tmp_path: Path,
) -> None:
    job = _capacity_job()
    first = _run_flow(tmp_path / "first", "closed", job, run_id="2" * 32)
    second = _run_flow(tmp_path / "second", "closed", job, run_id="3" * 32)
    first_result = first.nodes["solve"].artifacts["result"]
    second_result = second.nodes["solve"].artifacts["result"]
    first_evidence = first.nodes["solve"].artifacts["closure-evidence"]
    second_evidence = second.nodes["solve"].artifacts["closure-evidence"]

    assert first_result.path.read_bytes() == second_result.path.read_bytes()
    assert first_evidence.path.read_bytes() == second_evidence.path.read_bytes()
    assert first_result.qualifiers == second_result.qualifiers
    assert first_evidence.qualifiers == second_evidence.qualifiers


def test_reference_adapter_rejects_result_from_a_different_reference_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _capacity_job()
    stale_result = run(_state_budget_job())
    monkeypatch.setattr(
        "sigilicon.experimental.workflows.reference_physical_design.run",
        lambda _job: stale_result,
    )

    flow = _run_flow(tmp_path, "policy-mismatch", job, run_id="5" * 32)
    outcome = flow.nodes["solve"]

    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "failed"
    assert outcome.artifacts == {}
    assert outcome.reason is not None
    assert "complete reference job" in outcome.reason


@pytest.mark.parametrize(
    ("tamper", "message"),
    (
        ("job", "provenance does not match"),
        ("backend", "different backend"),
        ("deterministic", "deterministic execution"),
        ("closed", "closure evidence disagrees"),
    ),
)
def test_reference_adapter_rejects_tampered_result_provenance_and_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    message: str,
) -> None:
    job = _capacity_job()
    result = run(job)
    if tamper == "job":
        result = replace(
            result,
            provenance=replace(result.provenance, job_identity="wrong-job"),
        )
    elif tamper == "backend":
        result = replace(
            result,
            provenance=replace(result.provenance, backend="different-backend"),
        )
    elif tamper == "deterministic":
        result = replace(
            result,
            provenance=replace(result.provenance, deterministic=False),
        )
    else:
        result = replace(result, closed=False)
    monkeypatch.setattr(
        "sigilicon.experimental.workflows.reference_physical_design.run",
        lambda _job: result,
    )

    flow = _run_flow(tmp_path, tamper, job, run_id="4" * 32)
    outcome = flow.nodes["solve"]

    assert outcome.execution_status == "succeeded"
    assert outcome.result_status == "failed"
    assert outcome.artifacts == {}
    assert outcome.reason is not None and message in outcome.reason
