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


__all__ = [
    "CanonicalSerializationError",
    "canonical_json",
    "canonical_sha256",
    "physical_design_job_from_json",
    "physical_design_result_from_json",
    "placement_routing_closure_evidence_from_json",
]
