"""Receipt-bound evidence for PEX, post-layout analysis, and qualification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sigilicon.canonical import canonical_from_json, canonical_json
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    VerificationCompletion,
)
from sigilicon.identifiers import bounded_identity


class PexStatus(str, Enum):
    EXTRACTED = "extracted"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"


class PhysicalAnalysisStatus(str, Enum):
    PASSED = "passed"
    VIOLATED = "violated"
    UNSUPPORTED = "unsupported"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class DerivedArtifactIdentity:
    role: str
    kind: str
    identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("derived artifact role must be non-empty")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("derived artifact kind must be non-empty")
        bounded_identity(self.identity, "derived artifact identity")


@dataclass(frozen=True)
class PhysicalAnalysisFinding:
    identity: str
    count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, str)
            or not self.identity
            or type(self.count) is not int
            or self.count <= 0
        ):
            raise ValueError("physical-analysis finding needs an identity and count")


def _validate_unexecuted(
    status: PexStatus | PhysicalAnalysisStatus,
    completion: VerificationCompletion,
    label: str,
) -> None:
    if status in {
        PexStatus.UNSUPPORTED,
        PexStatus.BACKEND_UNAVAILABLE,
        PhysicalAnalysisStatus.UNSUPPORTED,
        PhysicalAnalysisStatus.BACKEND_UNAVAILABLE,
    } and (completion.executed or completion.report_parsed or completion.exit_code is not None):
        raise ValueError(f"{status.value} {label} cannot claim execution")
    if status in {PexStatus.EXECUTION_FAILED, PhysicalAnalysisStatus.EXECUTION_FAILED}:
        if not completion.executed or completion.proven:
            raise ValueError(
                f"execution_failed {label} requires an incomplete execution"
            )


@dataclass(frozen=True)
class PexEvidence:
    status: PexStatus
    layout: CheckedLayoutIdentity
    source: CheckedSourceIdentity
    completion: VerificationCompletion
    parasitics: DerivedArtifactIdentity | None
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, PexStatus):
            raise ValueError("PEX status must be PexStatus")
        if not isinstance(self.layout, CheckedLayoutIdentity):
            raise ValueError("PEX evidence needs a checked layout identity")
        if not isinstance(self.source, CheckedSourceIdentity):
            raise ValueError("PEX evidence needs a checked source identity")
        if not isinstance(self.completion, VerificationCompletion):
            raise ValueError("PEX evidence needs verification completion")
        if self.parasitics is not None and not isinstance(
            self.parasitics, DerivedArtifactIdentity
        ):
            raise ValueError("PEX parasitics must be a typed artifact identity or null")
        if not isinstance(self.message, str):
            raise ValueError("PEX evidence message must be text")
        if self.status is PexStatus.EXTRACTED:
            if not self.completion.proven or self.parasitics is None:
                raise ValueError(
                    "extracted PEX requires parsed completion and parasitic artifact"
                )
        else:
            _validate_unexecuted(self.status, self.completion, "PEX")
            if self.parasitics is not None:
                raise ValueError(f"{self.status.value} PEX cannot issue parasitics")

    def canonical_json(self) -> str:
        return canonical_json(self)


def _validate_analysis(
    status: PhysicalAnalysisStatus,
    completion: VerificationCompletion,
    findings: tuple[PhysicalAnalysisFinding, ...],
    label: str,
) -> None:
    if not isinstance(status, PhysicalAnalysisStatus):
        raise ValueError(f"{label} status must be PhysicalAnalysisStatus")
    if not isinstance(findings, tuple) or any(
        not isinstance(item, PhysicalAnalysisFinding) for item in findings
    ):
        raise ValueError(f"{label} findings must be a typed tuple")
    finding_count = sum(item.count for item in findings)
    if status is PhysicalAnalysisStatus.PASSED:
        if not completion.proven or finding_count:
            raise ValueError(f"passed {label} requires parsed zero-finding completion")
    elif status is PhysicalAnalysisStatus.VIOLATED:
        if not completion.proven or finding_count <= 0:
            raise ValueError(
                f"violated {label} requires parsed positive findings"
            )
    else:
        _validate_unexecuted(status, completion, label)
        if finding_count:
            raise ValueError(f"{status.value} {label} cannot claim findings")


@dataclass(frozen=True)
class PostLayoutEvidence:
    status: PhysicalAnalysisStatus
    layout: CheckedLayoutIdentity
    source: CheckedSourceIdentity
    pex_evidence_identity: str
    parasitics_identity: str
    specification_identity: str
    completion: VerificationCompletion
    findings: tuple[PhysicalAnalysisFinding, ...]
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.layout, CheckedLayoutIdentity):
            raise ValueError("post-layout evidence needs a checked layout identity")
        if not isinstance(self.source, CheckedSourceIdentity):
            raise ValueError("post-layout evidence needs a checked source identity")
        bounded_identity(self.pex_evidence_identity, "PEX evidence identity")
        bounded_identity(self.parasitics_identity, "parasitics identity")
        bounded_identity(self.specification_identity, "post-layout specification identity")
        if not isinstance(self.completion, VerificationCompletion):
            raise ValueError("post-layout evidence needs verification completion")
        if not isinstance(self.message, str):
            raise ValueError("post-layout evidence message must be text")
        _validate_analysis(
            self.status,
            self.completion,
            self.findings,
            "post-layout analysis",
        )

    def canonical_json(self) -> str:
        return canonical_json(self)


@dataclass(frozen=True)
class QualificationEvidence:
    status: PhysicalAnalysisStatus
    layout: CheckedLayoutIdentity
    source: CheckedSourceIdentity
    drc_evidence_identity: str
    lvs_evidence_identity: str
    specification_identity: str
    completion: VerificationCompletion
    findings: tuple[PhysicalAnalysisFinding, ...]
    message: str
    pex_evidence_identity: str | None = None
    post_layout_evidence_identity: str | None = None
    area_dbu2: int | None = None
    power_femtowatts: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.layout, CheckedLayoutIdentity):
            raise ValueError("qualification evidence needs a checked layout identity")
        if not isinstance(self.source, CheckedSourceIdentity):
            raise ValueError("qualification evidence needs a checked source identity")
        bounded_identity(self.drc_evidence_identity, "DRC evidence identity")
        bounded_identity(self.lvs_evidence_identity, "LVS evidence identity")
        bounded_identity(self.specification_identity, "qualification specification identity")
        if self.pex_evidence_identity is not None:
            bounded_identity(self.pex_evidence_identity, "PEX evidence identity")
        if self.post_layout_evidence_identity is not None:
            bounded_identity(
                self.post_layout_evidence_identity,
                "post-layout evidence identity",
            )
        for label, value in (
            ("area", self.area_dbu2),
            ("power", self.power_femtowatts),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(
                    f"qualified {label} must be a non-negative integer or null"
                )
        if not isinstance(self.completion, VerificationCompletion):
            raise ValueError("qualification evidence needs verification completion")
        if not isinstance(self.message, str):
            raise ValueError("qualification evidence message must be text")
        _validate_analysis(
            self.status,
            self.completion,
            self.findings,
            "qualification",
        )

    def canonical_json(self) -> str:
        return canonical_json(self)


def pex_evidence_id(evidence: PexEvidence) -> str:
    return (
        f"{evidence.layout.owner}:{evidence.layout.name}:"
        f"pex:{evidence.completion.backend}"
    )


def post_layout_evidence_id(evidence: PostLayoutEvidence) -> str:
    return (
        f"{evidence.layout.owner}:{evidence.layout.name}:"
        f"post-layout:{evidence.specification_identity}"
    )


def qualification_evidence_id(evidence: QualificationEvidence) -> str:
    return (
        f"{evidence.layout.owner}:{evidence.layout.name}:"
        f"qualification:{evidence.specification_identity}"
    )


def pex_evidence_from_json(text: str) -> PexEvidence:
    return canonical_from_json(text, PexEvidence)


def post_layout_evidence_from_json(text: str) -> PostLayoutEvidence:
    return canonical_from_json(text, PostLayoutEvidence)


def qualification_evidence_from_json(text: str) -> QualificationEvidence:
    return canonical_from_json(text, QualificationEvidence)


__all__ = [
    "DerivedArtifactIdentity",
    "PexEvidence",
    "PexStatus",
    "PhysicalAnalysisFinding",
    "PhysicalAnalysisStatus",
    "PostLayoutEvidence",
    "QualificationEvidence",
    "pex_evidence_id",
    "pex_evidence_from_json",
    "post_layout_evidence_id",
    "post_layout_evidence_from_json",
    "qualification_evidence_id",
    "qualification_evidence_from_json",
]
