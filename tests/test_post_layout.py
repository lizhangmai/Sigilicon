from __future__ import annotations

import pytest

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
from sigilicon.flow.post_layout import PEX_NETLIST_KIND


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
