from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import struct

from sigilicon.domain.physical_verification import (
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
    ExecutionEnvironment,
    ExecutionProfile,
    FlowEngine,
    FlowNode,
    FlowSpec,
    FlowTarget,
    LVS_ACTION,
    LVS_EVIDENCE_KIND,
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
    PHYSICAL_VERIFICATION_POLICY_KIND,
    PHYSICAL_DESIGN_ACTION,
    PHYSICAL_DESIGN_JOB_KIND,
    PHYSICAL_MATERIALIZATION_ACTION,
    PHYSICAL_MATERIALIZATION_PLAN_KIND,
    ProducedArtifact,
    REFERENCE_MATERIALIZATION_ADAPTER,
    REFERENCE_PNR_ADAPTER,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
)
from sigilicon.layout.materialization_execution import (
    MaterializationCompletion,
    MaterializationExecutionStatus,
)
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
from sigilicon.workflows.layout_verification import (
    load_receipt_bound_verification_inputs,
)
from sigilicon.workflows.physical_design import (
    collect_materialization_execution_result,
    materialization_execution_facts,
    read_materialization_execution_request,
    write_materialization_receipt,
)


_BENCHMARK_INPUT_ACTION = "benchmark.closure-inputs"
_BENCHMARK_INPUT_ADAPTER = "benchmark-closure-inputs"
_BENCHMARK_LAYOUT_ADAPTER = "benchmark-materialize-layout"
_BENCHMARK_DRC_ADAPTER = "benchmark-drc-parser"
_BENCHMARK_LVS_ADAPTER = "benchmark-lvs-parser"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


_BENCHMARK_ENVIRONMENT = ExecutionEnvironment(
    capabilities={
        "tool.layout-materializer": ResolvedCapability(
            "benchmark.contract-materializer"
        ),
        "tool.calibre": ResolvedCapability("benchmark.parsed-report-fixture"),
    },
    platform_assets=(
        ResolvedPlatformAsset(
            "physical-layout",
            "platform.layout-view-set",
            "benchmark.layout-assets",
            (
                ResolvedPlatformAssetMember("layer-map", Path("/contract/layer-map")),
                ResolvedPlatformAssetMember(
                    "master-layouts", Path("/contract/master-layouts")
                ),
            ),
        ),
        ResolvedPlatformAsset(
            "physical-verification",
            "platform.calibre-verification",
            "benchmark.verification-decks",
            (
                ResolvedPlatformAssetMember("drc-deck", Path("/contract/drc-deck")),
                ResolvedPlatformAssetMember("lvs-deck", Path("/contract/lvs-deck")),
            ),
        ),
    ),
)


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
            ".SUBCKT campaign-layout source sink\n.ENDS campaign-layout\n",
            encoding="utf-8",
        )
        context.output_path(
            "verification-policy", "physical-verification.toml"
        ).write_text(
            '''schema = 1
contract_kind = "physical-verification-policy"
path_scope = "owner"
owner = "benchmark"

[drc]
disabled_defines = {}
configuration_warnings = []
waiver_layers = []
''',
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        source = context.output_path("source", "canonical-source.cdl")
        policy = context.output_path(
            "verification-policy", "physical-verification.toml"
        )
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
                    source,
                    qualifiers={
                        "owner": "benchmark",
                        "name": "campaign-layout",
                        "source-sha256": _sha256_file(source),
                    },
                ),
                ProducedArtifact(
                    "verification-policy",
                    PHYSICAL_VERIFICATION_POLICY_KIND,
                    policy,
                    qualifiers={
                        "owner": "benchmark",
                        "policy-sha256": _sha256_file(policy),
                    },
                ),
            ),
        )


def _gds_record(record_type: int, data_type: int = 0, data: bytes = b"") -> bytes:
    assert len(data) % 2 == 0
    return struct.pack(">HBB", len(data) + 4, record_type, data_type) + data


def _gds_text(value: str) -> bytes:
    payload = value.encode("ascii")
    return payload if len(payload) % 2 == 0 else payload + b"\0"


def _benchmark_gds(plan) -> bytes:
    """Plan-derived GDSII contract evidence; never a signoff layout."""

    segment = plan.route_segments[0]
    return b"".join(
        (
            _gds_record(0x00, 0x02, struct.pack(">H", 600)),
            _gds_record(0x01, 0x02, bytes(24)),
            _gds_record(0x02, 0x06, _gds_text("CAMPAIGN-CONTRACT")),
            _gds_record(0x03, 0x05, bytes(16)),
            _gds_record(0x05, 0x02, bytes(24)),
            _gds_record(0x06, 0x06, _gds_text("campaign-layout")),
            _gds_record(0x09),
            _gds_record(0x0D, 0x02, struct.pack(">H", 1)),
            _gds_record(0x0E, 0x02, struct.pack(">H", 0)),
            _gds_record(0x0F, 0x03, struct.pack(">i", segment.width_dbu)),
            _gds_record(
                0x10,
                0x03,
                struct.pack(
                    ">iiii",
                    segment.start.x,
                    segment.start.y,
                    segment.end.x,
                    segment.end.y,
                ),
            ),
            _gds_record(0x11),
            _gds_record(0x07),
            _gds_record(0x04),
        )
    )


class _BenchmarkLayoutAdapter:
    """Unregistered materialization contract fixture, never product evidence."""

    def __init__(self, *, corrupt_result_identity: bool = False) -> None:
        self._corrupt_result_identity = corrupt_result_identity

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            _job, _result, plan, _target = read_materialization_execution_request(
                context
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            return (str(exc),)
        return () if plan.executable else ("plan is diagnostic-only",)

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        _job, _result, plan, _target = read_materialization_execution_request(
            context
        )
        layout = context.output_path("layout", "layout.gds")
        layout.write_bytes(_benchmark_gds(plan))
        receipt = write_materialization_receipt(
            context,
            status=MaterializationExecutionStatus.MATERIALIZED,
            completion=MaterializationCompletion(
                "benchmark.contract-materializer",
                True,
                True,
                True,
                0,
            ),
            layout_path=layout,
            message="benchmark contract materialization evidence",
        )
        return AdapterExecution.succeeded(
            details=materialization_execution_facts(receipt)
        )

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        collected = collect_materialization_execution_result(context, execution)
        if not self._corrupt_result_identity:
            return collected
        artifacts = tuple(
            replace(
                artifact,
                qualifiers={**artifact.qualifiers, "result-sha256": "0" * 64},
            )
            if artifact.role == "layout"
            else artifact
            for artifact in collected.artifacts
        )
        return replace(collected, artifacts=artifacts)


class _BenchmarkVerificationAdapter:
    """Test-only typed report fixture; it is never package-registered."""

    def _status(self, context: ActionContext) -> PhysicalVerificationStatus:
        return PhysicalVerificationStatus(str(context.action_config["outcome"]))

    def _evidence(self, context: ActionContext) -> DrcEvidence | LvsEvidence:
        status = self._status(context)
        completion = VerificationCompletion(
            "benchmark-parsed-report-fixture",
            True,
            True,
            0,
        )
        inputs = load_receipt_bound_verification_inputs(context)
        layout = inputs.layout
        if context.action.kind == DRC_ACTION:
            violations = (
                ()
                if status is PhysicalVerificationStatus.CLEAN
                else (DrcViolation("M1.W.1", 1),)
            )
            return DrcEvidence(status, layout, completion, violations, "fixture DRC")
        assert inputs.source is not None
        mismatches = (
            ()
            if status is PhysicalVerificationStatus.CLEAN
            else (LvsMismatch("INCORRECT", 1),)
        )
        return LvsEvidence(
            status,
            layout,
            inputs.source,
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
                        "receipt-sha256": str(evidence.layout.receipt_sha256),
                        "job-sha256": str(evidence.layout.job_sha256),
                        "plan-sha256": evidence.layout.plan_sha256,
                        "result-sha256": str(evidence.layout.result_sha256),
                        **(
                            {"source-sha256": evidence.source.artifact_sha256}
                            if isinstance(evidence, LvsEvidence)
                            else {}
                        ),
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
                ArtifactPort(
                    "verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND
                ),
            ),
            adapters=(_BENCHMARK_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(_BENCHMARK_INPUT_ADAPTER, _BenchmarkInputsAdapter(job))
    registry.register_action_adapter(
        PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
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
                    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
                    config={
                        "target": {
                            "owner": "benchmark",
                            "name": "campaign-layout",
                            "format": "gdsii",
                        }
                    },
                    bindings=(
                        ArtifactBinding("job", "inputs", "job"),
                        ArtifactBinding("result", "solve", "result"),
                        ArtifactBinding("plan", "compile", "plan"),
                    ),
                ),
                FlowNode(
                    "drc",
                    DRC_ACTION,
                    config={"outcome": drc},
                    bindings=(
                        ArtifactBinding("layout", "layout", "layout"),
                        ArtifactBinding("receipt", "layout", "receipt"),
                        ArtifactBinding(
                            "verification-policy",
                            "inputs",
                            "verification-policy",
                        ),
                    ),
                ),
                FlowNode(
                    "lvs",
                    LVS_ACTION,
                    config={"outcome": lvs},
                    bindings=(
                        ArtifactBinding("layout", "layout", "layout"),
                        ArtifactBinding("receipt", "layout", "receipt"),
                        ArtifactBinding("source", "inputs", "source"),
                        ArtifactBinding(
                            "verification-policy",
                            "inputs",
                            "verification-policy",
                        ),
                    ),
                ),
            )
        )
        selections.extend(
            (
                AdapterSelection(
                    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
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


def _runner(engine: FlowEngine, *, artifact_root: Path) -> ClosureCampaignRunner:
    return ClosureCampaignRunner(
        engine,
        artifact_root=artifact_root,
        environment=_BENCHMARK_ENVIRONMENT,
    )


def test_campaign_closes_typed_reference_flow_deterministically(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="clean")
    campaign = _campaign(plan, bindings)

    first = _runner(
        engine,
        artifact_root=tmp_path / "first",
    ).run(campaign)
    second = _runner(
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
    fixed = _runner(
        fixed_engine,
        artifact_root=tmp_path / "fixed",
    ).run(_campaign(fixed_plan, fixed_bindings, scope=scope))
    budget_engine, budget_plan, budget_bindings = _flow(
        _gridless_job(maximum_route_states=1)
    )
    budget = _runner(
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
    state_limited = _runner(
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
    iteration_limited = _runner(
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
    repair = _runner(
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
    valid = _runner(
        valid_engine,
        artifact_root=tmp_path / "valid",
    ).run(_campaign(valid_plan, valid_bindings))
    invalid_engine, invalid_plan, invalid_bindings = _flow(
        _gridless_job(),
        drc="clean",
        lvs="clean",
        corrupt_layout_identity=True,
    )
    invalid = _runner(
        invalid_engine,
        artifact_root=tmp_path / "invalid",
    ).run(_campaign(invalid_plan, invalid_bindings))

    assert valid.termination is ClosureCampaignTermination.REPAIR
    assert valid.final_quality.identity is ClosureStageStatus.SATISFIED
    assert invalid.termination is ClosureCampaignTermination.EXECUTION_FAILED
    assert invalid.final_quality.identity is ClosureStageStatus.INVALID_IDENTITY


def test_required_future_analysis_is_explicitly_unsupported(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="clean")
    result = _runner(
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
