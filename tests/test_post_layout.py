from __future__ import annotations

from pathlib import Path

import pytest

from conftest import StagedAdapterFixture
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    VerificationCompletion,
)
from sigilicon.domain.post_layout import (
    DerivedArtifactIdentity,
    PexEvidence,
    PexStatus,
    PhysicalAnalysisFinding,
    PhysicalAnalysisStatus,
    PostLayoutEvidence,
    QualificationEvidence,
    pex_evidence_from_json,
    post_layout_evidence_from_json,
    qualification_evidence_from_json,
)
from sigilicon.flow import (
    ActionBinding,
    ActionContract,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
)
from sigilicon.flow.physical_design import (
    MATERIALIZED_GDS_KIND,
    MATERIALIZATION_RECEIPT_KIND,
)
from sigilicon.flow.physical_verification import (
    CANONICAL_SOURCE_NETLIST_KIND,
)
from sigilicon.flow.post_layout import (
    PEX_ACTION,
    PEX_EVIDENCE_KIND,
    PEX_NETLIST_KIND,
    PHYSICAL_QUALIFICATION_ACTION,
    POST_LAYOUT_ACTION,
    register_post_layout_actions,
)


_SOURCE_ACTION = "fixture.post-layout-inputs"
_SOURCE_ADAPTER = "fixture-post-layout-inputs"
_PEX_ADAPTER = "fixture-pex"


class _NoopAdapter(StagedAdapterFixture):
    def validate_inputs(self, context) -> tuple[str, ...]:
        return ()

    def prepare(self, context) -> None:
        pass

    def execute(self, context) -> AdapterExecution:
        return AdapterExecution.succeeded()

    def collect_result(self, context, execution) -> CollectedActionResult:
        return CollectedActionResult()


def _layout() -> CheckedLayoutIdentity:
    return CheckedLayoutIdentity(
        "layout-fixture",
        "plan-fixture",
        "result-fixture",
        "owner",
        "cell",
        "source-fixture",
        "policy-fixture",
        "gdsii",
    )


def _source() -> CheckedSourceIdentity:
    return CheckedSourceIdentity("checked-source", "owner", "cell")


def _completion(*, proven: bool = True) -> VerificationCompletion:
    return VerificationCompletion(
        "fixture-backend",
        True,
        proven,
        0 if proven else 1,
    )


def test_receipt_bound_downstream_evidence_round_trips_without_metric_dicts() -> None:
    parasitics = DerivedArtifactIdentity("parasitics", PEX_NETLIST_KIND, "parasitics-fixture")
    pex = PexEvidence(
        PexStatus.EXTRACTED,
        _layout(),
        _source(),
        _completion(),
        parasitics,
        "validated extraction",
    )
    post_layout = PostLayoutEvidence(
        PhysicalAnalysisStatus.PASSED,
        _layout(),
        _source(),
        "post-layout-run",
        parasitics.identity,
        "post-layout-policy",
        _completion(),
        (),
        "specification passed",
    )
    qualification = QualificationEvidence(
        PhysicalAnalysisStatus.VIOLATED,
        _layout(),
        _source(),
        "layout-drift",
        "source-drift",
        "parasitics-drift",
        _completion(),
        (PhysicalAnalysisFinding("timing", 1),),
        "one canonical requirement failed",
        "post-layout-run",
        "evidence-drift",
        123,
        456,
    )

    assert pex_evidence_from_json(pex.canonical_json()) == pex
    assert post_layout_evidence_from_json(post_layout.canonical_json()) == post_layout
    assert qualification_evidence_from_json(
        qualification.canonical_json()
    ) == qualification


def test_downstream_success_cannot_be_inferred_from_exit_or_artifact_alone() -> None:
    with pytest.raises(ValueError, match="parsed completion"):
        PexEvidence(
            PexStatus.EXTRACTED,
            _layout(),
            _source(),
            _completion(proven=False),
            DerivedArtifactIdentity("parasitics", PEX_NETLIST_KIND, "parasitics-fixture"),
            "unparsed",
        )
    with pytest.raises(ValueError, match="positive findings"):
        PostLayoutEvidence(
            PhysicalAnalysisStatus.VIOLATED,
            _layout(),
            _source(),
            "post-layout-run",
            "parasitics-fixture",
            "post-layout-policy",
            _completion(),
            (),
            "no authoritative finding",
        )
    unavailable = PexEvidence(
        PexStatus.BACKEND_UNAVAILABLE,
        _layout(),
        _source(),
        VerificationCompletion("missing-backend", False, False, None),
        None,
        "backend unavailable",
    )
    failed = PexEvidence(
        PexStatus.EXECUTION_FAILED,
        _layout(),
        _source(),
        _completion(proven=False),
        None,
        "backend execution failed",
    )
    assert unavailable.status is PexStatus.BACKEND_UNAVAILABLE
    assert failed.status is PexStatus.EXECUTION_FAILED


def test_post_layout_actions_are_owner_extensions_with_explicit_preflight() -> None:
    registry = FlowRegistry()
    register_post_layout_actions(registry)
    pex_contract = registry.action(PEX_ACTION)
    assert pex_contract.adapters == ()
    assert pex_contract.adapter_extensible
    assert pex_contract.required_capabilities == ("tool.pex",)
    assert pex_contract.platform_assets[0].members == (
        "pex-deck",
        "pex-support-root",
    )
    assert registry.action(POST_LAYOUT_ACTION).adapters == ()
    assert registry.action(PHYSICAL_QUALIFICATION_ACTION).adapters == ()

    registry.register_action(
        ActionContract(
            _SOURCE_ACTION,
            outputs=(
                ArtifactPort("layout", MATERIALIZED_GDS_KIND),
                ArtifactPort("receipt", MATERIALIZATION_RECEIPT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
            ),
            adapters=(_SOURCE_ADAPTER,),
        )
    )
    registry.register_adapter(_SOURCE_ADAPTER, _NoopAdapter())
    registry.register_action_adapter(PEX_ACTION, _PEX_ADAPTER, _NoopAdapter())
    engine = FlowEngine(registry)
    spec = FlowSpec(
        owner="owner",
        flow_id="pex-preflight",
        recipe_id="pex-preflight-recipe",
        nodes=(
            FlowNode("inputs", _SOURCE_ACTION),
            FlowNode(
                "pex",
                PEX_ACTION,
                bindings=(
                    ArtifactBinding("layout", "inputs", "layout"),
                    ArtifactBinding("receipt", "inputs", "receipt"),
                    ArtifactBinding("source", "inputs", "source"),
                ),
            ),
        ),
        targets=(FlowTarget("pex", ("pex",)),),
        action_bindings=(
            ActionBinding(_SOURCE_ACTION, _SOURCE_ADAPTER),
            ActionBinding(
                PEX_ACTION,
                _PEX_ADAPTER,
                platform_assets={"physical-pex": "fixture.pex"},
            ),
        ),
    )
    plan = engine.plan(spec, "pex")
    asset = ResolvedPlatformAsset(
        "physical-pex",
        "platform.pex",
        "fixture.pex",
        (
            ResolvedPlatformAssetMember("pex-deck", Path("/fixture/pex.deck")),
            ResolvedPlatformAssetMember(
                "pex-support-root", Path("/fixture/pex-support")
            ),
        ),
    )
    ready = engine.preflight(
        plan,
        ExecutionEnvironment(
            {"tool.pex": ResolvedCapability("fixture-pex")},
            (asset,),
        ),
    )
    blocked = engine.preflight(plan, ExecutionEnvironment({}, (asset,)))
    incomplete_asset = ResolvedPlatformAsset(
        "physical-pex",
        "platform.pex",
        "fixture.pex",
        (ResolvedPlatformAssetMember("pex-deck", Path("/fixture/pex.deck")),),
    )
    missing_support = engine.preflight(
        plan,
        ExecutionEnvironment(
            {"tool.pex": ResolvedCapability("fixture-pex")},
            (incomplete_asset,),
        ),
    )

    assert ready.status == "ready"
    assert blocked.status == "blocked"
    assert ("tool.pex", "missing") in {
        (check.requirement, check.status) for check in blocked.checks
    }
    assert missing_support.status == "blocked"
    assert any(
        check.requirement == "physical-pex"
        and check.status == "incomplete"
        for check in missing_support.checks
    )
