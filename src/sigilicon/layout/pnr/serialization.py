"""Strict reversible serialization for the public physical-design Interface."""

from __future__ import annotations

from sigilicon.canonical import (
    CanonicalSerializationError,
    canonical_from_json,
    canonical_json,
)
from sigilicon.layout.pnr.model import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    PlacementRoutingClosureEvidence,
)


def physical_design_job_from_json(text: str) -> PhysicalDesignJob:
    return canonical_from_json(text, PhysicalDesignJob)


def physical_design_result_from_json(text: str) -> PhysicalDesignResult:
    return canonical_from_json(text, PhysicalDesignResult)


def placement_routing_closure_evidence_from_json(
    text: str,
) -> PlacementRoutingClosureEvidence:
    return canonical_from_json(text, PlacementRoutingClosureEvidence)


def physical_design_job_id(job: PhysicalDesignJob) -> str:
    return f"physical-design-job:{job.technology.name}:{job.design.name}"


def physical_design_result_id(result: PhysicalDesignResult) -> str:
    return result.artifact_id


def physical_closure_evidence_id(evidence: PlacementRoutingClosureEvidence) -> str:
    return evidence.artifact_id


__all__ = [
    "CanonicalSerializationError",
    "canonical_json",
    "physical_closure_evidence_id",
    "physical_design_job_from_json",
    "physical_design_job_id",
    "physical_design_result_from_json",
    "physical_design_result_id",
    "placement_routing_closure_evidence_from_json",
]
