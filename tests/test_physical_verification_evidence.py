from __future__ import annotations

import hashlib

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
from sigilicon.workflows.layout_verification import (
    drc_evidence_from_summary,
    lvs_evidence_from_report,
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
