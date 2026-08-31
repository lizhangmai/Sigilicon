"""Serialization for reference-only PNR values."""

from __future__ import annotations

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
from sigilicon.layout.physical_design_serialization import (
    physical_design_job_id,
    physical_design_result_id,
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


__all__ = [
    "CanonicalSerializationError",
    "canonical_json",
    "physical_closure_evidence_id",
    "physical_design_job_id",
    "reference_pnr_job_from_json",
    "reference_pnr_result_from_json",
    "physical_design_result_id",
    "placement_routing_closure_evidence_from_json",
]
