"""Serialization for reference-only PNR values."""

from __future__ import annotations

from hashlib import sha256

from sigilicon.canonical import (
    CanonicalSerializationError,
    canonical_from_json,
    canonical_json,
)
from sigilicon.experimental.reference_pnr.model import (
    PlacementRoutingClosureEvidence,
    ReferencePnrJob,
    ReferencePnrResult,
)


def reference_pnr_job_from_json(text: str) -> ReferencePnrJob:
    return canonical_from_json(text, ReferencePnrJob)


def reference_pnr_result_from_json(text: str) -> ReferencePnrResult:
    return canonical_from_json(text, ReferencePnrResult)


def placement_routing_closure_evidence_from_json(
    text: str,
) -> PlacementRoutingClosureEvidence:
    return canonical_from_json(text, PlacementRoutingClosureEvidence)


def physical_closure_evidence_id(evidence: PlacementRoutingClosureEvidence) -> str:
    return evidence.artifact_id


def reference_pnr_job_id(job: ReferencePnrJob) -> str:
    """Identify the complete reference-solver job, including its policy."""

    digest = sha256(canonical_json(job).encode("utf-8")).hexdigest()
    return (
        f"reference-pnr-job:{job.technology.name}:{job.design.name}:"
        f"sha256:{digest}"
    )


def reference_pnr_result_id(result: ReferencePnrResult) -> str:
    """Return the reference-solver result identity carried by a result."""

    return result.artifact_id


__all__ = [
    "CanonicalSerializationError",
    "canonical_json",
    "physical_closure_evidence_id",
    "reference_pnr_job_id",
    "reference_pnr_job_from_json",
    "reference_pnr_result_id",
    "reference_pnr_result_from_json",
    "placement_routing_closure_evidence_from_json",
]
