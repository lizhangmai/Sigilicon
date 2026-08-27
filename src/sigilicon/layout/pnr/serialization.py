"""Strict reversible serialization for the public physical-design Interface."""

from __future__ import annotations

from sigilicon.layout.pnr._serialization import (
    CanonicalSerializationError,
    canonical_from_json,
    canonical_json,
    canonical_sha256,
)
from sigilicon.layout.pnr.model import (
    PhysicalDesignJob,
    PhysicalDesignResult,
    PnrExecutionPolicy,
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


def physical_design_intent_sha256(job: PhysicalDesignJob) -> str:
    return canonical_sha256(
        {
            "technology": job.technology,
            "design": job.design,
            "constraints": job.constraints,
            "request": job.request,
            "routing_constraints": job.routing_constraints,
        }
    )


def pnr_execution_sha256(policy: PnrExecutionPolicy) -> str:
    return canonical_sha256(policy)


__all__ = [
    "CanonicalSerializationError",
    "canonical_json",
    "canonical_sha256",
    "physical_design_job_from_json",
    "physical_design_intent_sha256",
    "physical_design_result_from_json",
    "placement_routing_closure_evidence_from_json",
    "pnr_execution_sha256",
]
