from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import struct

import pytest

from conftest import StagedAdapterFixture
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
    drc_evidence_id,
    lvs_evidence_from_json,
    lvs_evidence_id,
)
from sigilicon.domain.post_layout import (
    DerivedArtifactIdentity,
    PexEvidence,
    PexStatus,
    PhysicalAnalysisStatus,
    PostLayoutEvidence,
    QualificationEvidence,
    pex_evidence_from_json,
    pex_evidence_id,
    post_layout_evidence_from_json,
    post_layout_evidence_id,
    qualification_evidence_from_json,
    qualification_evidence_id,
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
    PEX_ACTION,
    PEX_EVIDENCE_KIND,
    PEX_NETLIST_KIND,
    PHYSICAL_QUALIFICATION_ACTION,
    PHYSICAL_QUALIFICATION_EVIDENCE_KIND,
    PHYSICAL_QUALIFICATION_SPEC_KIND,
    PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
    PHYSICAL_VERIFICATION_POLICY_KIND,
    POST_LAYOUT_ACTION,
    POST_LAYOUT_EVIDENCE_KIND,
    POST_LAYOUT_SPEC_KIND,
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
    PhysicalOwnerIdentity,
    PhysicalOwnerKind,
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
    physical_design_job_id,
)
from sigilicon.workflows.builtin import build_flow_registry
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
from sigilicon.workflows.closure_repair import (
    ClosureRepairPolicy,
    PlacementRepairDirective,
    apply_repair_plan,
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
_BENCHMARK_PEX_ADAPTER = "benchmark-pex-parser"
_BENCHMARK_POST_LAYOUT_ADAPTER = "benchmark-post-layout-parser"
_BENCHMARK_QUALIFICATION_ADAPTER = "benchmark-qualification-parser"


def _identity_bytes(value: bytes) -> str:
    return f"fixture-bytes:{len(value)}:{value[:4].hex()}"


def _identity_file(path: Path) -> str:
    return f"fixture:{path.name}"


def _typed_identity(path: Path, parser) -> str:
    value = parser(path.read_text(encoding="utf-8"))
    if isinstance(value, DrcEvidence):
        return drc_evidence_id(value)
    if isinstance(value, LvsEvidence):
        return lvs_evidence_id(value)
    if isinstance(value, PexEvidence):
        return pex_evidence_id(value)
    if isinstance(value, PostLayoutEvidence):
        return post_layout_evidence_id(value)
    if isinstance(value, QualificationEvidence):
        return qualification_evidence_id(value)
    raise AssertionError("unexpected typed evidence")


_BENCHMARK_ENVIRONMENT = ExecutionEnvironment(
    capabilities={
        "tool.layout-materializer": ResolvedCapability(
            "benchmark.contract-materializer"
        ),
        "tool.calibre": ResolvedCapability("benchmark.parsed-report-fixture"),
        "tool.pex": ResolvedCapability("benchmark.parsed-pex-fixture"),
        "tool.post-layout-simulation": ResolvedCapability(
            "benchmark.parsed-post-layout-fixture"
        ),
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
            "physical-pex",
            "platform.pex",
            "benchmark.pex-assets",
            (
                ResolvedPlatformAssetMember("pex-deck", Path("/contract/pex-deck")),
                ResolvedPlatformAssetMember(
                    "pex-support-root", Path("/contract/pex-support")
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


def _repairable_drc_job() -> PhysicalDesignJob:
    job = _gridless_job()
    return replace(
        job,
        design=replace(
            job.design,
            routing_blockages=(
                RoutingBlockage(
                    "repairable-keepout",
                    2,
                    2,
                    (LayerShape("route", Rect(0, 0, 2, 2)),),
                    Placement(Point(0, 6)),
                    repair_region=Rect(0, 5, 20, 10),
                ),
            ),
        ),
    )


def _drc_repair_policy() -> ClosureRepairPolicy:
    return ClosureRepairPolicy(
        "benchmark",
        "benchmark-drc-repair",
        2,
        (
            PlacementRepairDirective(
                ClosureFeedbackKind.DRC_RULE,
                "M1.W.1",
                PhysicalOwnerIdentity(
                    PhysicalOwnerKind.BLOCKAGE,
                    ("repairable-keepout",),
                ),
                Placement(Point(2, 6)),
                Rect(0, 5, 20, 10),
                2,
            ),
        ),
    )


class _BenchmarkInputsAdapter(StagedAdapterFixture):
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
        context.output_path("post-layout-specification", "post-layout.toml").write_text(
            'schema = 1\nowner = "benchmark"\n',
            encoding="utf-8",
        )
        context.output_path(
            "qualification-specification", "qualification.toml"
        ).write_text(
            'schema = 1\nowner = "benchmark"\n',
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
        post_layout_spec = context.output_path(
            "post-layout-specification", "post-layout.toml"
        )
        qualification_spec = context.output_path(
            "qualification-specification", "qualification.toml"
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
                        "source-identity": _identity_file(source),
                    },
                ),
                ProducedArtifact(
                    "verification-policy",
                    PHYSICAL_VERIFICATION_POLICY_KIND,
                    policy,
                    qualifiers={
                        "owner": "benchmark",
                        "policy-identity": _identity_file(policy),
                    },
                ),
                ProducedArtifact(
                    "post-layout-specification",
                    POST_LAYOUT_SPEC_KIND,
                    post_layout_spec,
                    qualifiers={"specification-identity": _identity_file(post_layout_spec)},
                ),
                ProducedArtifact(
                    "qualification-specification",
                    PHYSICAL_QUALIFICATION_SPEC_KIND,
                    qualification_spec,
                    qualifiers={
                        "specification-identity": _identity_file(qualification_spec)
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


class _BenchmarkLayoutAdapter(StagedAdapterFixture):
    """Unregistered materialization contract fixture, never product evidence."""

    def __init__(
        self,
        *,
        outcome: str = "materialized",
        corrupt_identity: str | None = None,
    ) -> None:
        self._outcome = MaterializationExecutionStatus(outcome)
        self._corrupt_identity = corrupt_identity

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
        if self._outcome is MaterializationExecutionStatus.MATERIALIZED:
            layout = context.output_path("layout", "layout.gds")
            layout.write_bytes(_benchmark_gds(plan))
            completion = MaterializationCompletion(
                "benchmark.contract-materializer",
                True,
                True,
                True,
                0,
            )
        elif self._outcome is MaterializationExecutionStatus.EXECUTION_FAILED:
            layout = None
            completion = MaterializationCompletion(
                "benchmark.contract-materializer",
                True,
                False,
                False,
                1,
            )
        else:
            layout = None
            completion = MaterializationCompletion(
                "benchmark.contract-materializer",
                False,
                False,
                False,
                None,
            )
        receipt = write_materialization_receipt(
            context,
            status=self._outcome,
            completion=completion,
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
        if self._corrupt_identity is None:
            return collected
        artifacts = tuple(
            replace(
                artifact,
                qualifiers={
                    **artifact.qualifiers,
                    {
                        "result": "result-identity",
                        "receipt": "receipt-identity",
                        "layout": "layout-identity",
                    }[self._corrupt_identity]: "corrupt-identity",
                },
            )
            if artifact.role
            == ("receipt" if self._corrupt_identity == "receipt" else "layout")
            else artifact
            for artifact in collected.artifacts
        )
        return replace(collected, artifacts=artifacts)


class _BenchmarkVerificationAdapter(StagedAdapterFixture):
    """Test-only typed report fixture; it is never package-registered."""

    def _status(self, context: ActionContext) -> PhysicalVerificationStatus:
        return PhysicalVerificationStatus(str(context.action_config["outcome"]))

    def _evidence(self, context: ActionContext) -> DrcEvidence | LvsEvidence:
        status = self._status(context)
        if status in {
            PhysicalVerificationStatus.CLEAN,
            PhysicalVerificationStatus.VIOLATED,
        }:
            completion = VerificationCompletion(
                "benchmark-parsed-report-fixture",
                True,
                True,
                0,
            )
        elif status is PhysicalVerificationStatus.EXECUTION_FAILED:
            completion = VerificationCompletion(
                "benchmark-parsed-report-fixture",
                True,
                False,
                1,
            )
        else:
            completion = VerificationCompletion(
                "benchmark-parsed-report-fixture",
                False,
                False,
                None,
            )
        inputs = load_receipt_bound_verification_inputs(context)
        layout = inputs.layout
        if context.action.kind == DRC_ACTION:
            violations = (
                ()
                if status is PhysicalVerificationStatus.CLEAN
                else (DrcViolation("M1.W.1", 1),)
                if status is PhysicalVerificationStatus.VIOLATED
                else ()
            )
            return DrcEvidence(status, layout, completion, violations, "fixture DRC")
        assert inputs.source is not None
        mismatches = (
            ()
            if status is PhysicalVerificationStatus.CLEAN
            else (LvsMismatch("INCORRECT", 1),)
            if status is PhysicalVerificationStatus.VIOLATED
            else ()
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
        return ()

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
                        "layout-identity": evidence.layout.artifact_identity,
                        "receipt-identity": str(evidence.layout.receipt_identity),
                        "job-identity": str(evidence.layout.job_identity),
                        "plan-identity": evidence.layout.plan_identity,
                        "result-identity": str(evidence.layout.result_identity),
                        **(
                            {"source-identity": evidence.source.artifact_identity}
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


def _downstream_subject(context: ActionContext):
    layout = context.input("layout")
    source = context.input("source")
    return (
        CheckedLayoutIdentity(
            str(layout.qualifiers["layout-identity"]),
            str(layout.qualifiers["plan-identity"]),
            str(layout.qualifiers["result-identity"]),
            str(layout.qualifiers["owner"]),
            str(layout.qualifiers["name"]),
            str(layout.qualifiers["receipt-identity"]),
            str(layout.qualifiers["job-identity"]),
            str(layout.qualifiers["format"]),
        ),
        CheckedSourceIdentity(
            str(source.qualifiers["source-identity"]),
            str(source.qualifiers["owner"]),
            str(source.qualifiers["name"]),
        ),
    )


class _BenchmarkDownstreamAdapter(StagedAdapterFixture):
    """Test-only parsed downstream evidence; never package-registered."""

    def __init__(self, *, corrupt_layout_identity: bool = False) -> None:
        self._corrupt_layout_identity = corrupt_layout_identity

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        try:
            self._evidence(context)
        except (KeyError, OSError, ValueError, TypeError) as exc:
            return (str(exc),)
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def _evidence(
        self,
        context: ActionContext,
    ) -> PexEvidence | PostLayoutEvidence | QualificationEvidence:
        layout, source = _downstream_subject(context)
        completion = VerificationCompletion(
            "benchmark-parsed-downstream-fixture",
            True,
            True,
            0,
        )
        if context.action.kind == PEX_ACTION:
            parasitics = context.output_path("parasitics", "extracted.pex")
            if not parasitics.exists():
                parasitics.write_text("* validated fixture parasitics\n", encoding="utf-8")
            return PexEvidence(
                PexStatus.EXTRACTED,
                replace(layout, artifact_identity="corrupt-identity")
                if self._corrupt_layout_identity
                else layout,
                source,
                completion,
                DerivedArtifactIdentity(
                    "parasitics",
                    PEX_NETLIST_KIND,
                    _identity_file(parasitics),
                ),
                "fixture PEX",
            )
        pex_identity = _typed_identity(
            context.input("pex").path, pex_evidence_from_json
        )
        if context.action.kind == POST_LAYOUT_ACTION:
            parasitics_identity = str(
                context.input("parasitics").qualifiers["parasitics-identity"]
            )
            return PostLayoutEvidence(
                PhysicalAnalysisStatus.PASSED,
                layout,
                source,
                pex_identity,
                parasitics_identity,
                str(context.input("specification").qualifiers["specification-identity"]),
                completion,
                (),
                "fixture post-layout",
            )
        return QualificationEvidence(
            PhysicalAnalysisStatus.PASSED,
            layout,
            source,
            _typed_identity(context.input("drc").path, drc_evidence_from_json),
            _typed_identity(context.input("lvs").path, lvs_evidence_from_json),
            str(context.input("specification").qualifiers["specification-identity"]),
            completion,
            (),
            "fixture qualification",
            pex_identity,
            _typed_identity(
                context.input("post-layout").path, post_layout_evidence_from_json
            ),
            200,
            300,
        )

    def execute(self, context: ActionContext) -> AdapterExecution:
        evidence = self._evidence(context)
        context.output_path("evidence", "downstream-evidence.json").write_text(
            evidence.canonical_json(),
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        evidence_path = context.output_path("evidence", "downstream-evidence.json")
        if context.action.kind == PEX_ACTION:
            evidence = pex_evidence_from_json(
                evidence_path.read_text(encoding="utf-8")
            )
            evidence_identity = pex_evidence_id(evidence)
            assert evidence.parasitics is not None
            parasitics = context.output_path("parasitics", "extracted.pex")
            return CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "parasitics",
                        PEX_NETLIST_KIND,
                        parasitics,
                        qualifiers={
                            "pex-evidence-identity": evidence_identity,
                            "parasitics-identity": evidence.parasitics.identity,
                        },
                    ),
                    ProducedArtifact(
                        "evidence",
                        PEX_EVIDENCE_KIND,
                        evidence_path,
                        qualifiers={
                            "layout-identity": evidence.layout.artifact_identity,
                            "receipt-identity": evidence.layout.receipt_identity,
                            "job-identity": evidence.layout.job_identity,
                            "result-identity": evidence.layout.result_identity,
                            "plan-identity": evidence.layout.plan_identity,
                            "source-identity": evidence.source.artifact_identity,
                            "status": evidence.status.value,
                            "evidence-identity": evidence_identity,
                        },
                    ),
                ),
                facts={"pex-status": evidence.status.value, "pex-completed": True},
            )
        if context.action.kind == POST_LAYOUT_ACTION:
            evidence = post_layout_evidence_from_json(
                evidence_path.read_text(encoding="utf-8")
            )
            evidence_identity = post_layout_evidence_id(evidence)
            kind = POST_LAYOUT_EVIDENCE_KIND
            qualifiers = {
                "layout-identity": evidence.layout.artifact_identity,
                "receipt-identity": evidence.layout.receipt_identity,
                "source-identity": evidence.source.artifact_identity,
                "pex-evidence-identity": evidence.pex_evidence_identity,
                "parasitics-identity": evidence.parasitics_identity,
                "specification-identity": evidence.specification_identity,
                "status": evidence.status.value,
                "evidence-identity": evidence_identity,
            }
            facts = {
                "post-layout-status": evidence.status.value,
                "post-layout-passed": True,
            }
        else:
            evidence = qualification_evidence_from_json(
                evidence_path.read_text(encoding="utf-8")
            )
            evidence_identity = qualification_evidence_id(evidence)
            kind = PHYSICAL_QUALIFICATION_EVIDENCE_KIND
            qualifiers = {
                "layout-identity": evidence.layout.artifact_identity,
                "receipt-identity": evidence.layout.receipt_identity,
                "source-identity": evidence.source.artifact_identity,
                "drc-evidence-identity": evidence.drc_evidence_identity,
                "lvs-evidence-identity": evidence.lvs_evidence_identity,
                "pex-evidence-identity": evidence.pex_evidence_identity,
                "post-layout-evidence-identity": (
                    evidence.post_layout_evidence_identity
                ),
                "specification-identity": evidence.specification_identity,
                "status": evidence.status.value,
                "evidence-identity": evidence_identity,
            }
            facts = {
                "qualification-status": evidence.status.value,
                "qualification-passed": True,
            }
        return CollectedActionResult(
            artifacts=(ProducedArtifact("evidence", kind, evidence_path, qualifiers),),
            facts=facts,
        )


def _flow(
    job: PhysicalDesignJob,
    *,
    drc: str | None = None,
    lvs: str | None = None,
    corrupt_layout_identity: bool = False,
    materialization: str = "materialized",
    corrupt_identity: str | None = None,
    downstream: bool = False,
    corrupt_downstream_identity: bool = False,
):
    if corrupt_layout_identity:
        corrupt_identity = "result"
    registry = build_flow_registry()
    registry.register_action(
        ActionContract(
            _BENCHMARK_INPUT_ACTION,
            outputs=(
                ArtifactPort("job", PHYSICAL_DESIGN_JOB_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
                ArtifactPort(
                    "verification-policy", PHYSICAL_VERIFICATION_POLICY_KIND
                ),
                ArtifactPort(
                    "post-layout-specification",
                    POST_LAYOUT_SPEC_KIND,
                ),
                ArtifactPort(
                    "qualification-specification",
                    PHYSICAL_QUALIFICATION_SPEC_KIND,
                ),
            ),
            adapters=(_BENCHMARK_INPUT_ADAPTER,),
        )
    )
    registry.register_adapter(_BENCHMARK_INPUT_ADAPTER, _BenchmarkInputsAdapter(job))
    registry.register_action_adapter(
        PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
        _BENCHMARK_LAYOUT_ADAPTER,
        _BenchmarkLayoutAdapter(
            outcome=materialization,
            corrupt_identity=corrupt_identity,
        ),
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
    registry.register_action_adapter(
        PEX_ACTION,
        _BENCHMARK_PEX_ADAPTER,
        _BenchmarkDownstreamAdapter(
            corrupt_layout_identity=corrupt_downstream_identity
        ),
    )
    registry.register_action_adapter(
        POST_LAYOUT_ACTION,
        _BENCHMARK_POST_LAYOUT_ADAPTER,
        _BenchmarkDownstreamAdapter(),
    )
    registry.register_action_adapter(
        PHYSICAL_QUALIFICATION_ACTION,
        _BENCHMARK_QUALIFICATION_ADAPTER,
        _BenchmarkDownstreamAdapter(),
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
            materialization_receipt=CampaignArtifactReference("layout", "receipt"),
            layout=CampaignArtifactReference("layout", "layout"),
            source=CampaignArtifactReference("inputs", "source"),
            drc=CampaignArtifactReference("drc", "evidence"),
            lvs=CampaignArtifactReference("lvs", "evidence"),
        )

    if downstream:
        assert drc is not None and lvs is not None
        nodes.extend(
            (
                FlowNode(
                    "pex",
                    PEX_ACTION,
                    bindings=(
                        ArtifactBinding("layout", "layout", "layout"),
                        ArtifactBinding("receipt", "layout", "receipt"),
                        ArtifactBinding("source", "inputs", "source"),
                    ),
                ),
                FlowNode(
                    "post-layout",
                    POST_LAYOUT_ACTION,
                    bindings=(
                        ArtifactBinding("layout", "layout", "layout"),
                        ArtifactBinding("receipt", "layout", "receipt"),
                        ArtifactBinding("source", "inputs", "source"),
                        ArtifactBinding("pex", "pex", "evidence"),
                        ArtifactBinding("parasitics", "pex", "parasitics"),
                        ArtifactBinding(
                            "specification",
                            "inputs",
                            "post-layout-specification",
                        ),
                    ),
                ),
                FlowNode(
                    "qualification",
                    PHYSICAL_QUALIFICATION_ACTION,
                    bindings=(
                        ArtifactBinding("layout", "layout", "layout"),
                        ArtifactBinding("receipt", "layout", "receipt"),
                        ArtifactBinding("source", "inputs", "source"),
                        ArtifactBinding("drc", "drc", "evidence"),
                        ArtifactBinding("lvs", "lvs", "evidence"),
                        ArtifactBinding("pex", "pex", "evidence"),
                        ArtifactBinding(
                            "post-layout", "post-layout", "evidence"
                        ),
                        ArtifactBinding(
                            "specification",
                            "inputs",
                            "qualification-specification",
                        ),
                    ),
                ),
            )
        )
        selections.extend(
            (
                AdapterSelection(
                    PEX_ACTION,
                    _BENCHMARK_PEX_ADAPTER,
                    platform_asset_identities={
                        "physical-pex": "benchmark.pex-assets"
                    },
                ),
                AdapterSelection(
                    POST_LAYOUT_ACTION,
                    _BENCHMARK_POST_LAYOUT_ADAPTER,
                ),
                AdapterSelection(
                    PHYSICAL_QUALIFICATION_ACTION,
                    _BENCHMARK_QUALIFICATION_ADAPTER,
                ),
            )
        )
        goals = ("qualification",)
        bindings = replace(
            bindings,
            parasitics=CampaignArtifactReference("pex", "parasitics"),
            pex=CampaignArtifactReference("pex", "evidence"),
            post_layout_specification=CampaignArtifactReference(
                "inputs", "post-layout-specification"
            ),
            post_layout=CampaignArtifactReference("post-layout", "evidence"),
            qualification_specification=CampaignArtifactReference(
                "inputs", "qualification-specification"
            ),
            qualification=CampaignArtifactReference("qualification", "evidence"),
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
    repair_policy: ClosureRepairPolicy | None = None,
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
        repair_policy,
    )


def _runner(engine: FlowEngine, *, artifact_root: Path) -> ClosureCampaignRunner:
    return ClosureCampaignRunner(
        engine,
        artifact_root=artifact_root,
        environment=_BENCHMARK_ENVIRONMENT,
    )


def test_public_dbu_campaign_closes_full_receipt_bound_graph_deterministically(
    tmp_path: Path,
) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="clean")
    campaign = _campaign(plan, bindings)

    assert tuple(item.node.action_kind for item in plan.nodes) == (
        _BENCHMARK_INPUT_ACTION,
        PHYSICAL_DESIGN_ACTION,
        PHYSICAL_MATERIALIZATION_ACTION,
        PHYSICAL_MATERIALIZATION_EXECUTION_ACTION,
        DRC_ACTION,
        LVS_ACTION,
    )

    first = _runner(
        engine,
        artifact_root=tmp_path / "first",
    ).run(campaign)
    second = _runner(
        engine,
        artifact_root=tmp_path / "second",
    ).run(campaign)

    assert first.termination is ClosureCampaignTermination.CLOSED
    assert first.final_quality != replace(
        first.final_quality,
        drc=ClosureStageStatus.VIOLATED,
        drc_findings=1,
    )
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
        "materialization-receipt",
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


def test_campaign_requires_explicit_receipt_reference_without_node_scanning(
    tmp_path: Path,
) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="clean")
    missing_receipt = replace(bindings, materialization_receipt=None)
    result = _runner(engine, artifact_root=tmp_path / "missing-receipt").run(
        _campaign(plan, missing_receipt)
    )

    assert result.iterations[0].flow_status == "failed"
    assert result.termination is ClosureCampaignTermination.EXECUTION_FAILED
    assert result.final_quality.identity is ClosureStageStatus.INVALID_IDENTITY


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
    job = _repairable_drc_job()
    policy = _drc_repair_policy()
    engine, plan, bindings = _flow(job, drc="violated", lvs="clean")
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
            repair_policy=policy,
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
            repair_policy=policy,
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
            repair_policy=policy,
        )
    )

    assert state_limited.termination is ClosureCampaignTermination.STATE_BUDGET
    assert state_limited.iterations[0].repair_plan is not None
    assert state_limited.iterations[0].repair_plan.accepted
    assert iteration_limited.termination is (
        ClosureCampaignTermination.ITERATION_BUDGET
    )
    assert iteration_limited.iterations[0].repair_plan is not None
    assert iteration_limited.iterations[0].repair_plan.accepted
    assert repair.termination is ClosureCampaignTermination.REPAIR
    assert all(item.flow_status == "accepted" for item in repair.iterations)
    assert not repair.closed
    assert len(repair.iterations) == 2
    assert repair.iterations[1].quality_decision is ClosureQualityDecision.EQUIVALENT
    assert repair.final_quality.drc is ClosureStageStatus.VIOLATED
    assert len(repair.iterations[-1].feedback) == 1
    feedback = repair.iterations[-1].feedback[0]
    assert feedback.kind is ClosureFeedbackKind.DRC_RULE
    assert feedback.identities == ("M1.W.1",)
    assert feedback.repairable
    repair_plan = repair.iterations[-1].repair_plan
    assert repair_plan is not None and repair_plan.accepted
    next_job = apply_repair_plan(job, repair_plan)
    assert next_job.design.routing_blockages[0].placement == Placement(Point(2, 6))
    assert next_job.repair_lineage is not None
    assert next_job.repair_lineage.parent_job_identity == physical_design_job_id(job)
    assert closure_campaign_result_from_json(repair.canonical_json()) == repair


def test_lvs_violation_produces_only_typed_mismatch_feedback(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(_gridless_job(), drc="clean", lvs="violated")
    result = _runner(engine, artifact_root=tmp_path / "lvs-violated").run(
        _campaign(plan, bindings)
    )

    assert result.termination is ClosureCampaignTermination.UNSUPPORTED
    assert result.iterations[0].flow_status == "accepted"
    assert result.final_quality.lvs is ClosureStageStatus.VIOLATED
    assert result.iterations[0].feedback == (
        result.iterations[0].feedback[0],
    )
    feedback = result.iterations[0].feedback[0]
    assert feedback.kind is ClosureFeedbackKind.LVS_MISMATCH
    assert feedback.identities == ("INCORRECT",)
    assert len(feedback.source_evidence) == 2
    assert result.iterations[0].repair_plan is None


@pytest.mark.parametrize(
    "materialization,expected_status,expected_termination",
    (
        (
            "unsupported",
            ClosureStageStatus.UNSUPPORTED,
            ClosureCampaignTermination.UNSUPPORTED,
        ),
        (
            "backend_unavailable",
            ClosureStageStatus.BACKEND_UNAVAILABLE,
            ClosureCampaignTermination.UNSUPPORTED,
        ),
        (
            "execution_failed",
            ClosureStageStatus.EXECUTION_FAILED,
            ClosureCampaignTermination.EXECUTION_FAILED,
        ),
    ),
)
def test_campaign_preserves_non_materialized_receipt_status(
    tmp_path: Path,
    materialization: str,
    expected_status: ClosureStageStatus,
    expected_termination: ClosureCampaignTermination,
) -> None:
    engine, plan, bindings = _flow(
        _gridless_job(),
        drc="clean",
        lvs="clean",
        materialization=materialization,
    )
    result = _runner(
        engine,
        artifact_root=tmp_path / materialization,
    ).run(_campaign(plan, bindings))

    assert result.termination is expected_termination
    assert result.final_quality.materialization is expected_status
    assert result.final_quality.drc is ClosureStageStatus.NOT_EVALUATED
    assert result.final_quality.lvs is ClosureStageStatus.NOT_EVALUATED
    assert result.iterations[0].feedback[0].kind is (
        ClosureFeedbackKind.MATERIALIZATION
    )


@pytest.mark.parametrize(
    "stage,status,expected_stage,expected_termination",
    (
        (
            "drc",
            "backend_unavailable",
            ClosureStageStatus.BACKEND_UNAVAILABLE,
            ClosureCampaignTermination.UNSUPPORTED,
        ),
        (
            "drc",
            "execution_failed",
            ClosureStageStatus.EXECUTION_FAILED,
            ClosureCampaignTermination.EXECUTION_FAILED,
        ),
        (
            "lvs",
            "unsupported",
            ClosureStageStatus.UNSUPPORTED,
            ClosureCampaignTermination.UNSUPPORTED,
        ),
        (
            "lvs",
            "execution_failed",
            ClosureStageStatus.EXECUTION_FAILED,
            ClosureCampaignTermination.EXECUTION_FAILED,
        ),
    ),
)
def test_campaign_preserves_verification_nonconclusions(
    tmp_path: Path,
    stage: str,
    status: str,
    expected_stage: ClosureStageStatus,
    expected_termination: ClosureCampaignTermination,
) -> None:
    engine, plan, bindings = _flow(
        _gridless_job(),
        drc=status if stage == "drc" else "clean",
        lvs=status if stage == "lvs" else "clean",
    )
    result = _runner(
        engine,
        artifact_root=tmp_path / f"{stage}-{status}",
    ).run(_campaign(plan, bindings))

    assert result.termination is expected_termination
    assert getattr(result.final_quality, stage) is expected_stage
    assert result.iterations[0].flow_status == "accepted"


@pytest.mark.parametrize("corrupt", ("receipt", "layout", "result"))
def test_campaign_rejects_each_materialization_identity_link(
    tmp_path: Path,
    corrupt: str,
) -> None:
    engine, plan, bindings = _flow(
        _gridless_job(),
        drc="clean",
        lvs="clean",
        corrupt_identity=corrupt,
    )
    result = _runner(
        engine,
        artifact_root=tmp_path / f"corrupt-{corrupt}",
    ).run(_campaign(plan, bindings))

    assert result.termination is ClosureCampaignTermination.EXECUTION_FAILED
    if corrupt == "layout":
        assert result.iterations[0].flow_status == "failed"
    else:
        assert result.final_quality.identity is ClosureStageStatus.INVALID_IDENTITY
    assert not result.closed


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

    assert valid.termination is ClosureCampaignTermination.UNSUPPORTED
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
            scope=ClosureCampaignScope(
                require_pex=True,
                require_post_layout=True,
                require_qualification=True,
            ),
        )
    )

    assert result.termination is ClosureCampaignTermination.UNSUPPORTED
    assert result.final_quality.pex is ClosureStageStatus.UNSUPPORTED
    assert result.final_quality.post_layout is ClosureStageStatus.UNSUPPORTED
    assert result.final_quality.qualification is ClosureStageStatus.UNSUPPORTED
    assert not result.closed


def test_campaign_consumes_receipt_bound_downstream_evidence(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(
        _gridless_job(),
        drc="clean",
        lvs="clean",
        downstream=True,
    )
    scope = ClosureCampaignScope(
        require_pex=True,
        require_post_layout=True,
        require_qualification=True,
    )
    result = _runner(engine, artifact_root=tmp_path / "downstream").run(
        _campaign(plan, bindings, scope=scope)
    )

    assert result.termination is ClosureCampaignTermination.CLOSED
    assert result.final_quality.pex is ClosureStageStatus.SATISFIED
    assert result.final_quality.post_layout is ClosureStageStatus.SATISFIED
    assert result.final_quality.qualification is ClosureStageStatus.SATISFIED
    assert result.final_quality.cost.area_dbu2 == 200
    assert result.final_quality.cost.power_femtowatts == 300
    assert {item.label for item in result.iterations[0].provenance.artifacts} >= {
        "parasitics",
        "pex",
        "post-layout-specification",
        "post-layout",
        "qualification-specification",
        "qualification",
    }


def test_campaign_rejects_downstream_subject_identity_drift(tmp_path: Path) -> None:
    engine, plan, bindings = _flow(
        _gridless_job(),
        drc="clean",
        lvs="clean",
        downstream=True,
        corrupt_downstream_identity=True,
    )
    result = _runner(engine, artifact_root=tmp_path / "downstream-drift").run(
        _campaign(
            plan,
            bindings,
            scope=ClosureCampaignScope(
                require_pex=True,
                require_post_layout=True,
                require_qualification=True,
            ),
        )
    )

    assert result.termination is ClosureCampaignTermination.EXECUTION_FAILED
    assert result.final_quality.identity is ClosureStageStatus.INVALID_IDENTITY
    assert "PEX checked layout artifact identity mismatch" in result.message
