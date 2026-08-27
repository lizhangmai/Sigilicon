from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    DrcViolation,
    LvsEvidence,
    LvsMismatch,
    PhysicalVerificationStatus,
    VerificationCompletion,
    drc_evidence_from_json,
    lvs_evidence_from_json,
)
from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    CANONICAL_SOURCE_NETLIST_KIND,
    CollectedActionResult,
    DRC_ACTION,
    DRC_EVIDENCE_KIND,
    ExecutionProfile,
    FlowEngine,
    FlowNode,
    FlowSpec,
    FlowTarget,
    LVS_ACTION,
    LVS_EVIDENCE_KIND,
    MATERIALIZED_LAYOUT_KIND,
    PHYSICAL_DESIGN_ACTION,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_MATERIALIZATION_ACTION,
    PHYSICAL_MATERIALIZATION_PLAN_KIND,
    ProducedArtifact,
    REFERENCE_MATERIALIZATION_ADAPTER,
    REFERENCE_PNR_ADAPTER,
)
from sigilicon.layout.materialization import materialization_plan_from_json
from sigilicon.layout.pnr import (
    Axis,
    GridlessRoutingResource,
    LayerKind,
    LayerShape,
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
    Placement,
    PnrExecutionPolicy,
    PnrRequest,
    PnrStage,
    Point,
    Rect,
    RoutingBlockage,
    RoutingDirection,
    RoutingTrackPattern,
    canonical_sha256,
)
from sigilicon.workflows.builtin import builtin_workflow_registry
from sigilicon.workflows.closure_campaign import (
    CampaignArtifactReference,
    ClosureArtifactBindings,
    ClosureCampaign,
    ClosureCampaignRunner,
    ClosureCampaignScope,
    ClosureCampaignTermination,
    ClosureFeedbackKind,
    ClosureIteration,
    ClosureQualityDecision,
    ClosureStageStatus,
    closure_campaign_result_from_json,
    compare_closure_quality,
)


_BENCHMARK_INPUT_ACTION = "benchmark.closure-inputs"
_BENCHMARK_INPUT_ADAPTER = "benchmark-closure-inputs"
_BENCHMARK_LAYOUT_ACTION = "benchmark.materialize-layout"
_BENCHMARK_LAYOUT_ADAPTER = "benchmark-materialize-layout"
_BENCHMARK_DRC_ADAPTER = "benchmark-drc-parser"
_BENCHMARK_LVS_ADAPTER = "benchmark-lvs-parser"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _gridless_job(*, maximum_route_states: int = 200_000) -> PhysicalDesignJob:
    technology = PhysicalTechnology(
        "campaign-gridless",
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
            "campaign-closed",
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
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
        execution_policy=PnrExecutionPolicy(
            maximum_route_states=maximum_route_states
        ),
    )


def _fixed_blockage_job() -> PhysicalDesignJob:
    technology = PhysicalTechnology(
        "campaign-fixed-blocker",
        1000,
        1,
        layers=(
            PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.HORIZONTAL),
        ),
        routing_resources=(
            RoutingTrackPattern("only-track", "route", Axis.Y, 2, 4, 1),
        ),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 2),
        ),
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            "campaign-infeasible",
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
                    "fixed-blocker",
                    2,
                    2,
                    (LayerShape("route", Rect(0, 0, 2, 2)),),
                    Placement(Point(7, 0)),
                ),
            ),
        ),
        request=PnrRequest(stages=(PnrStage.PLACEMENT, PnrStage.ROUTING)),
    )


class _BenchmarkInputsAdapter:
    def __init__(self, job: PhysicalDesignJob) -> None:
        self._job = job

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        context.output_path("job", "physical-design-job.json").write_text(
            self._job.canonical_json(),
            encoding="utf-8",
        )
        context.output_path("source", "canonical-source.cdl").write_text(
            ".SUBCKT campaign source sink\n.ENDS campaign\n",
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "job",
                    PHYSICAL_DESIGN_JOB_KIND,
                    context.output_path("job", "physical-design-job.json"),
                ),
                ProducedArtifact(
                    "source",
                    CANONICAL_SOURCE_NETLIST_KIND,
                    context.output_path("source", "canonical-source.cdl"),
                    qualifiers={"owner": "benchmark", "name": "campaign-source"},
                ),
            ),
        )


class _BenchmarkLayoutAdapter:
    def __init__(self, *, corrupt_result_identity: bool = False) -> None:
        self._corrupt_result_identity = corrupt_result_identity

    def _plan(self, context: ActionContext):
        return materialization_plan_from_json(
            context.input("plan").path.read_text(encoding="utf-8")
        )

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            plan = self._plan(context)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            return (str(exc),)
        return () if plan.executable else ("plan is diagnostic-only",)

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        plan = self._plan(context)
        context.output_path("layout", "benchmark-layout.bin").write_bytes(
            b"benchmark-layout\n" + plan.canonical_json().encode("utf-8")
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        plan = self._plan(context)
        result_sha256 = plan.provenance.result_sha256
        if self._corrupt_result_identity:
            result_sha256 = "0" * 64
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "layout",
                    MATERIALIZED_LAYOUT_KIND,
                    context.output_path("layout", "benchmark-layout.bin"),
                    qualifiers={
                        "plan-sha256": canonical_sha256(plan),
                        "result-sha256": result_sha256,
                        "owner": "benchmark",
                        "name": "campaign-layout",
                    },
                ),
            ),
        )


class _BenchmarkVerificationAdapter:
    """Test-only typed report fixture; it is never package-registered."""

    def _status(self, context: ActionContext) -> PhysicalVerificationStatus:
        return PhysicalVerificationStatus(str(context.action_config["outcome"]))

    def _layout(self, context: ActionContext) -> CheckedLayoutIdentity:
        artifact = context.input("layout")
        return CheckedLayoutIdentity(
            _sha256_file(artifact.path),
            str(artifact.qualifiers["plan-sha256"]),
            str(artifact.qualifiers["result-sha256"]),
            str(artifact.qualifiers["owner"]),
            str(artifact.qualifiers["name"]),
        )

    def _evidence(self, context: ActionContext) -> DrcEvidence | LvsEvidence:
        status = self._status(context)
        completion = VerificationCompletion(
            "benchmark-parsed-report-fixture",
            True,
            True,
            0,
        )
        layout = self._layout(context)
        if context.action.kind == DRC_ACTION:
            violations = (
                ()
                if status is PhysicalVerificationStatus.CLEAN
                else (DrcViolation("M1.W.1", 1),)
            )
            return DrcEvidence(status, layout, completion, violations, "fixture DRC")
        source = context.input("source")
        mismatches = (
            ()
            if status is PhysicalVerificationStatus.CLEAN
            else (LvsMismatch("INCORRECT", 1),)
        )
        return LvsEvidence(
            status,
            layout,
            CheckedSourceIdentity(
                _sha256_file(source.path),
                str(source.qualifiers["owner"]),
                str(source.qualifiers["name"]),
            ),
            completion,
            mismatches,
            "fixture LVS",
        )

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            evidence = self._evidence(context)
        except (KeyError, ValueError, OSError) as exc:
            return (str(exc),)
        return () if evidence.status in {
            PhysicalVerificationStatus.CLEAN,
            PhysicalVerificationStatus.VIOLATED,
        } else ("fixture supports only clean and violated",)

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        evidence = self._evidence(context)
        context.output_path("evidence", "verification-evidence.json").write_text(
            evidence.canonical_json(),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        path = context.output_path("evidence", "verification-evidence.json")
        if context.action.kind == DRC_ACTION:
            evidence = drc_evidence_from_json(path.read_text(encoding="utf-8"))
            prefix = "drc"
            kind = DRC_EVIDENCE_KIND
        else:
            evidence = lvs_evidence_from_json(path.read_text(encoding="utf-8"))
            prefix = "lvs"
            kind = LVS_EVIDENCE_KIND
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "evidence",
                    kind,
                    path,
                    qualifiers={
                        "layout-sha256": evidence.layout.artifact_sha256,
                        "plan-sha256": evidence.layout.plan_sha256,
                        "status": evidence.status.value,
                    },
                ),
            ),
            facts={
                f"{prefix}-status": evidence.status.value,
                f"{prefix}-clean": evidence.clean,
                f"{prefix}-completed": evidence.completion.proven,
            },
        )


def _flow(
    job: PhysicalDesignJob,
    *,
    drc: str | None = None,
    lvs: str | None = None,
    corrupt_layout_identity: bool = False,
):
    registry = builtin_workflow_registry()
    registry.register_action(
        ActionContract(
            _BENCHMARK_INPUT_ACTION,
            outputs=(
                ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
            ),
            adapters=(_BENCHMARK_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(_BENCHMARK_INPUT_ADAPTER, _BenchmarkInputsAdapter(job))
    registry.register_action(
        ActionContract(
            _BENCHMARK_LAYOUT_ACTION,
            inputs=(ArtifactPort("plan", PHYSICAL_MATERIALIZATION_PLAN_KIND),),
            outputs=(ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),),
            adapters=(_BENCHMARK_LAYOUT_ADAPTER,),
        )
    )
    registry.register_adapter(
        _BENCHMARK_LAYOUT_ADAPTER,
        _BenchmarkLayoutAdapter(corrupt_result_identity=corrupt_layout_identity),
    )
    registry.register_action_adapter(
        DRC_ACTION,
        _BENCHMARK_DRC_ADAPTER,
        _BenchmarkVerificationAdapter(),
    )
    registry.register_action_adapter(
        LVS_ACTION,
        _BENCHMARK_LVS_ADAPTER,
        _BenchmarkVerificationAdapter(),
    )

    nodes = [
        FlowNode("inputs", _BENCHMARK_INPUT_ACTION),
        FlowNode(
            "solve",
            PHYSICAL_DESIGN_ACTION,
            bindings=(ArtifactBinding("job", "inputs", "job"),),
        ),
        FlowNode(
            "compile",
            PHYSICAL_MATERIALIZATION_ACTION,
            config={"target": {"owner": "benchmark", "name": "campaign-layout"}},
            bindings=(
                ArtifactBinding("job", "inputs", "job"),
                ArtifactBinding("result", "solve", "result", requires="valid"),
            ),
        ),
    ]
    selections = [
        AdapterSelection(_BENCHMARK_INPUT_ACTION, _BENCHMARK_INPUT_ADAPTER),
        AdapterSelection(PHYSICAL_DESIGN_ACTION, REFERENCE_PNR_ADAPTER),
        AdapterSelection(
            PHYSICAL_MATERIALIZATION_ACTION,
            REFERENCE_MATERIALIZATION_ADAPTER,
        ),
    ]
    goals = ("compile",)
    bindings = ClosureArtifactBindings(
        job=CampaignArtifactReference("inputs", "job"),
        result=CampaignArtifactReference("solve", "result"),
        materialization_plan=CampaignArtifactReference("compile", "plan"),
        closure_evidence=CampaignArtifactReference("solve", "closure-evidence"),
    )
    if drc is not None and lvs is not None:
        nodes.extend(
            (
                FlowNode(
                    "layout",
                    _BENCHMARK_LAYOUT_ACTION,
                    bindings=(ArtifactBinding("plan", "compile", "plan"),),
                ),
                FlowNode(
                    "drc",
                    DRC_ACTION,
                    config={"outcome": drc},
                    bindings=(ArtifactBinding("layout", "layout", "layout"),),
                ),
                FlowNode(
                    "lvs",
                    LVS_ACTION,
                    config={"outcome": lvs},
                    bindings=(
                        ArtifactBinding("layout", "layout", "layout"),
                        ArtifactBinding("source", "inputs", "source"),
                    ),
                ),
            )
        )
        selections.extend(
            (
                AdapterSelection(
                    _BENCHMARK_LAYOUT_ACTION,
                    _BENCHMARK_LAYOUT_ADAPTER,
                ),
                AdapterSelection(DRC_ACTION, _BENCHMARK_DRC_ADAPTER),
                AdapterSelection(LVS_ACTION, _BENCHMARK_LVS_ADAPTER),
            )
        )
        goals = ("drc", "lvs")
        bindings = replace(
            bindings,
            layout=CampaignArtifactReference("layout", "layout"),
            source=CampaignArtifactReference("inputs", "source"),
            drc=CampaignArtifactReference("drc", "evidence"),
            lvs=CampaignArtifactReference("lvs", "evidence"),
        )

    spec = FlowSpec(
        owner="benchmark",
        flow_id="typed-closure-campaign",
        nodes=tuple(nodes),
        targets=(FlowTarget("closure", goals),),
    )
    profile = ExecutionProfile(
        "benchmark",
        "reference-benchmark",
        tuple(selections),
    )
    engine = FlowEngine(registry)
    return engine, engine.plan(spec, "closure", profile), bindings


def _campaign(
    plan,
    bindings: ClosureArtifactBindings,
    *,
    count: int = 1,
    state_budget: int = 4,
    iteration_budget: int = 4,
    scope: ClosureCampaignScope = ClosureCampaignScope(),
) -> ClosureCampaign:
    return ClosureCampaign(
        "benchmark",
        "public-dbu-closure",
        tuple(
            ClosureIteration(f"round-{index}", plan, bindings)
            for index in range(count)
        ),
        state_budget,
        iteration_budget,
        scope,
    )


def test_campaign_closes_typed_reference_flow_deterministically(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="clean")
    campaign = _campaign(plan, bindings)

    first = ClosureCampaignRunner(
        engine,
        artifact_root=tmp_path / "first",
    ).run(campaign)
    second = ClosureCampaignRunner(
        engine,
        artifact_root=tmp_path / "second",
    ).run(campaign)

    assert first.termination is ClosureCampaignTermination.CLOSED
    assert first.closed
    assert first.final_quality.closed
    assert first.iterations[0].flow_status == "accepted"
    assert {item.label for item in first.iterations[0].provenance.artifacts} == {
        "closure-evidence",
        "drc",
        "job",
        "layout",
        "lvs",
        "materialization-plan",
        "result",
        "source",
    }
    assert first.canonical_json() == second.canonical_json()
    assert closure_campaign_result_from_json(first.canonical_json()) == first

    identity_failed = replace(
        first.final_quality,
        identity=ClosureStageStatus.INVALID_IDENTITY,
    )
    materialization_failed = replace(
        first.final_quality,
        materialization=ClosureStageStatus.VIOLATED,
    )
    drc_failed = replace(
        materialization_failed,
        drc=ClosureStageStatus.VIOLATED,
        drc_findings=1,
    )
    routing_failed = replace(
        materialization_failed,
        routing=replace(
            first.final_quality.routing,
            hard_blockers=1,
            closed=False,
        ),
    )

    assert compare_closure_quality(
        materialization_failed,
        identity_failed,
    ) is ClosureQualityDecision.IMPROVED
    assert compare_closure_quality(
        routing_failed,
        drc_failed,
    ) is ClosureQualityDecision.IMPROVED


def test_campaign_preserves_proven_infeasible_and_pnr_state_budget(
    tmp_path: Path,
) -> None:
    scope = ClosureCampaignScope(require_drc=False, require_lvs=False)
    fixed_engine, fixed_plan, fixed_bindings = _flow(_fixed_blockage_job())
    fixed = ClosureCampaignRunner(
        fixed_engine,
        artifact_root=tmp_path / "fixed",
    ).run(_campaign(fixed_plan, fixed_bindings, scope=scope))
    budget_engine, budget_plan, budget_bindings = _flow(
        _gridless_job(maximum_route_states=1)
    )
    budget = ClosureCampaignRunner(
        budget_engine,
        artifact_root=tmp_path / "budget",
    ).run(_campaign(budget_plan, budget_bindings, scope=scope))

    assert fixed.termination is ClosureCampaignTermination.PROVEN_INFEASIBLE
    assert fixed.iterations[0].feedback[0].kind is (
        ClosureFeedbackKind.FIXED_PHYSICAL_BLOCKER
    )
    assert not fixed.iterations[0].feedback[0].repairable
    assert budget.termination is ClosureCampaignTermination.STATE_BUDGET


def test_campaign_budgets_are_independent_and_feedback_is_attributed(
    tmp_path: Path,
) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="violated", lvs="clean")
    state_limited = ClosureCampaignRunner(
        engine,
        artifact_root=tmp_path / "state",
    ).run(
        _campaign(
            plan,
            bindings,
            count=2,
            state_budget=1,
            iteration_budget=3,
        )
    )
    iteration_limited = ClosureCampaignRunner(
        engine,
        artifact_root=tmp_path / "iteration",
    ).run(
        _campaign(
            plan,
            bindings,
            count=2,
            state_budget=3,
            iteration_budget=1,
        )
    )
    repair = ClosureCampaignRunner(
        engine,
        artifact_root=tmp_path / "repair",
    ).run(
        _campaign(
            plan,
            bindings,
            count=2,
            state_budget=3,
            iteration_budget=3,
        )
    )

    assert state_limited.termination is ClosureCampaignTermination.STATE_BUDGET
    assert iteration_limited.termination is (
        ClosureCampaignTermination.ITERATION_BUDGET
    )
    assert repair.termination is ClosureCampaignTermination.REPAIR
    assert len(repair.iterations) == 2
    assert repair.iterations[1].quality_decision is ClosureQualityDecision.EQUIVALENT
    assert repair.final_quality.drc is ClosureStageStatus.VIOLATED
    assert len(repair.iterations[-1].feedback) == 1
    feedback = repair.iterations[-1].feedback[0]
    assert feedback.kind is ClosureFeedbackKind.DRC_RULE
    assert feedback.identities == ("M1.W.1",)
    assert feedback.repairable


def test_campaign_distinguishes_invalid_identity_from_valid_nonclosure(
    tmp_path: Path,
) -> None:
    valid_engine, valid_plan, valid_bindings = _flow(
        _gridless_job(),
        drc="violated",
        lvs="clean",
    )
    valid = ClosureCampaignRunner(
        valid_engine,
        artifact_root=tmp_path / "valid",
    ).run(_campaign(valid_plan, valid_bindings))
    invalid_engine, invalid_plan, invalid_bindings = _flow(
        _gridless_job(),
        drc="clean",
        lvs="clean",
        corrupt_layout_identity=True,
    )
    invalid = ClosureCampaignRunner(
        invalid_engine,
        artifact_root=tmp_path / "invalid",
    ).run(_campaign(invalid_plan, invalid_bindings))

    assert valid.termination is ClosureCampaignTermination.REPAIR
    assert valid.final_quality.identity is ClosureStageStatus.SATISFIED
    assert invalid.termination is ClosureCampaignTermination.EXECUTION_FAILED
    assert invalid.final_quality.identity is ClosureStageStatus.INVALID_IDENTITY


def test_required_future_analysis_is_explicitly_unsupported(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="clean")
    result = ClosureCampaignRunner(
        engine,
        artifact_root=tmp_path / "future",
    ).run(
        _campaign(
            plan,
            bindings,
            scope=ClosureCampaignScope(require_pex=True),
        )
    )

    assert result.termination is ClosureCampaignTermination.UNSUPPORTED
    assert result.final_quality.pex is ClosureStageStatus.UNSUPPORTED
    assert not result.closed
