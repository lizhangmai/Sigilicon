from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    LvsEvidence,
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
    ExecutionProfile,
    FlowEngine,
    FlowNode,
    FlowSpec,
    FlowTarget,
    LVS_ACTION,
    MATERIALIZED_LAYOUT_KIND,
    OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
    PolicyCheck,
    PolicySpec,
    ProducedArtifact,
    builtin_registry,
)
from sigilicon.workflows.layout_verification import (
    drc_evidence_from_summary,
    lvs_evidence_from_report,
)
from sigilicon.workflows.physical_verification import (
    OfflinePhysicalVerificationAdapter,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _layout() -> CheckedLayoutIdentity:
    return CheckedLayoutIdentity(
        artifact_sha256=_digest(b"layout"),
        plan_sha256=_digest(b"plan"),
        result_sha256=_digest(b"result"),
        owner="benchmark",
        name="layout-candidate",
    )


def _source() -> CheckedSourceIdentity:
    return CheckedSourceIdentity(
        artifact_sha256=_digest(b"source"),
        owner="benchmark",
        name="canonical-source",
    )


def _drc_summary(*, violations: int) -> str:
    return f"""LAYER SRAMDMY ............ TOTAL Original Geometry Count = 0 (0)
RULECHECK CONFIG:WARNING .... TOTAL Result Count = 1 (1)
RULECHECK M1.W.1 .... TOTAL Result Count = {violations} ({violations})
TOTAL DRC Results Generated:     {violations + 1} ({violations + 1})
"""


def test_drc_parser_projects_clean_and_violated_typed_evidence() -> None:
    clean = drc_evidence_from_summary(
        _drc_summary(violations=0),
        layout=_layout(),
        backend="calibre",
        exit_code=0,
        configuration_warnings=("CONFIG:WARNING",),
        waiver_layers=("SRAMDMY",),
    )
    violated = drc_evidence_from_summary(
        _drc_summary(violations=3),
        layout=_layout(),
        backend="calibre",
        exit_code=0,
        configuration_warnings=("CONFIG:WARNING",),
        waiver_layers=("SRAMDMY",),
    )

    assert clean.status is PhysicalVerificationStatus.CLEAN
    assert clean.clean
    assert clean.completion.proven
    assert clean.violations == ()
    assert drc_evidence_from_json(clean.canonical_json()) == clean
    assert violated.status is PhysicalVerificationStatus.VIOLATED
    assert not violated.clean
    assert tuple((item.rule, item.count) for item in violated.violations) == (
        ("M1.W.1", 3),
    )


def test_lvs_parser_projects_checked_layout_and_source_identity() -> None:
    clean = lvs_evidence_from_report(
        "  CORRECT        top           top\n",
        primary="top",
        layout=_layout(),
        source=_source(),
        backend="calibre",
        exit_code=0,
    )
    violated = lvs_evidence_from_report(
        "  INCORRECT      top           top\n",
        primary="top",
        layout=_layout(),
        source=_source(),
        backend="calibre",
        exit_code=0,
    )

    assert clean.status is PhysicalVerificationStatus.CLEAN
    assert clean.source == _source()
    assert lvs_evidence_from_json(clean.canonical_json()) == clean
    assert violated.status is PhysicalVerificationStatus.VIOLATED
    assert tuple(item.category for item in violated.mismatches) == ("INCORRECT",)


def test_exit_code_zero_without_parsed_report_cannot_claim_clean() -> None:
    with pytest.raises(ValueError, match="parsed zero-finding completion"):
        DrcEvidence(
            PhysicalVerificationStatus.CLEAN,
            _layout(),
            VerificationCompletion("fake", True, False, 0),
            (),
            "not actually parsed",
        )


def test_evidence_serialization_rejects_drift_and_unknown_status() -> None:
    evidence = drc_evidence_from_summary(
        _drc_summary(violations=0),
        layout=_layout(),
        backend="calibre",
        exit_code=0,
        configuration_warnings=("CONFIG:WARNING",),
        waiver_layers=("SRAMDMY",),
    )

    with pytest.raises(ValueError, match="unknown=.*unexpected"):
        drc_evidence_from_json(
            evidence.canonical_json().replace(
                '  "message":',
                '  "unexpected": true,\n  "message":',
            )
        )
    with pytest.raises(ValueError, match="unknown PhysicalVerificationStatus"):
        drc_evidence_from_json(
            evidence.canonical_json().replace('"status": "clean"', '"status": "ok"')
        )


@pytest.mark.parametrize(
    "status,completion",
    (
        (
            PhysicalVerificationStatus.BACKEND_UNAVAILABLE,
            VerificationCompletion("fake", True, False, None),
        ),
        (
            PhysicalVerificationStatus.EXECUTION_FAILED,
            VerificationCompletion("fake", False, False, None),
        ),
    ),
)
def test_non_conclusive_statuses_cannot_misstate_execution(
    status: PhysicalVerificationStatus,
    completion: VerificationCompletion,
) -> None:
    with pytest.raises(ValueError):
        DrcEvidence(status, _layout(), completion, (), "invalid completion")


class _PhysicalInputsAdapter:
    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        context.output_path("layout", "layout.bin").write_bytes(b"layout")
        context.output_path("source", "source.cdl").write_bytes(b"source")
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "layout",
                    MATERIALIZED_LAYOUT_KIND,
                    context.output_path("layout", "layout.bin"),
                    qualifiers={
                        "plan-sha256": _digest(b"plan"),
                        "result-sha256": _digest(b"result"),
                        "owner": "benchmark",
                        "name": "layout-candidate",
                    },
                ),
                ProducedArtifact(
                    "source",
                    CANONICAL_SOURCE_NETLIST_KIND,
                    context.output_path("source", "source.cdl"),
                    qualifiers={
                        "owner": "benchmark",
                        "name": "canonical-source",
                    },
                ),
            ),
        )


def test_offline_flow_adapter_preserves_non_conclusive_statuses(
    tmp_path: Path,
) -> None:
    registry = builtin_registry()
    registry.register_action(
        ActionContract(
            "benchmark.physical-inputs",
            outputs=(
                ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
            ),
            adapters=("benchmark-physical-inputs",),
        )
    )
    registry.register_adapter("benchmark-physical-inputs", _PhysicalInputsAdapter())
    registry.register_adapter(
        OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
        OfflinePhysicalVerificationAdapter(),
    )
    spec = FlowSpec(
        owner="benchmark",
        flow_id="offline-physical-verification",
        nodes=(
            FlowNode("inputs", "benchmark.physical-inputs"),
            FlowNode(
                "drc",
                DRC_ACTION,
                bindings=(ArtifactBinding("layout", "inputs", "layout"),),
                policy="drc-clean",
            ),
            FlowNode(
                "lvs",
                LVS_ACTION,
                config={"outcome": "execution_failed"},
                bindings=(
                    ArtifactBinding("layout", "inputs", "layout"),
                    ArtifactBinding("source", "inputs", "source"),
                ),
                policy="lvs-clean",
            ),
        ),
        targets=(FlowTarget("verification", ("drc", "lvs")),),
        policies=(
            PolicySpec(
                "drc-clean",
                (PolicyCheck("clean", "drc-clean", "equals", True),),
            ),
            PolicySpec(
                "lvs-clean",
                (PolicyCheck("clean", "lvs-clean", "equals", True),),
            ),
        ),
    )
    profile = ExecutionProfile(
        "benchmark",
        "offline",
        (
            AdapterSelection(
                "benchmark.physical-inputs",
                "benchmark-physical-inputs",
            ),
            AdapterSelection(DRC_ACTION, OFFLINE_PHYSICAL_VERIFICATION_ADAPTER),
            AdapterSelection(LVS_ACTION, OFFLINE_PHYSICAL_VERIFICATION_ADAPTER),
        ),
    )
    result = FlowEngine(registry).run(
        FlowEngine(registry).plan(spec, "verification", profile),
        artifact_root=tmp_path / "artifacts",
        run_id="5" * 32,
    )
    drc = drc_evidence_from_json(
        result.nodes["drc"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )
    lvs = lvs_evidence_from_json(
        result.nodes["lvs"].artifacts["evidence"].path.read_text(encoding="utf-8")
    )

    assert result.status == "failed"
    assert result.nodes["drc"].execution_status == "succeeded"
    assert result.nodes["drc"].result_status == "valid"
    assert drc.status is PhysicalVerificationStatus.BACKEND_UNAVAILABLE
    assert not drc.clean
    assert result.nodes["lvs"].execution_status == "succeeded"
    assert result.nodes["lvs"].result_status == "valid"
    assert lvs.status is PhysicalVerificationStatus.EXECUTION_FAILED
    assert not lvs.completion.proven
    assert lvs.layout.artifact_sha256 == _digest(b"layout")
    assert lvs.source.artifact_sha256 == _digest(b"source")


def test_offline_adapter_refuses_to_fake_clean_evidence(tmp_path: Path) -> None:
    registry = builtin_registry()
    registry.register_action(
        ActionContract(
            "benchmark.physical-inputs",
            outputs=(
                ArtifactPort("layout", MATERIALIZED_LAYOUT_KIND),
                ArtifactPort("source", CANONICAL_SOURCE_NETLIST_KIND),
            ),
            adapters=("benchmark-physical-inputs",),
        )
    )
    registry.register_adapter("benchmark-physical-inputs", _PhysicalInputsAdapter())
    registry.register_adapter(
        OFFLINE_PHYSICAL_VERIFICATION_ADAPTER,
        OfflinePhysicalVerificationAdapter(),
    )
    spec = FlowSpec(
        owner="benchmark",
        flow_id="reject-fake-clean",
        nodes=(
            FlowNode("inputs", "benchmark.physical-inputs"),
            FlowNode(
                "drc",
                DRC_ACTION,
                config={"outcome": "clean"},
                bindings=(ArtifactBinding("layout", "inputs", "layout"),),
            ),
        ),
        targets=(FlowTarget("verification", ("drc",)),),
    )
    profile = ExecutionProfile(
        "benchmark",
        "offline",
        (
            AdapterSelection(
                "benchmark.physical-inputs",
                "benchmark-physical-inputs",
            ),
            AdapterSelection(DRC_ACTION, OFFLINE_PHYSICAL_VERIFICATION_ADAPTER),
        ),
    )

    result = FlowEngine(registry).run(
        FlowEngine(registry).plan(spec, "verification", profile),
        artifact_root=tmp_path / "artifacts",
        run_id="6" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["drc"].execution_status == "failed"
    assert result.nodes["drc"].artifacts == {}
    assert "cannot claim clean or violated" in (result.nodes["drc"].reason or "")
