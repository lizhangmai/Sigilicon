"""Strict serialization and identities for the stable physical-design contract."""

from __future__ import annotations

from hashlib import sha256

from sigilicon.canonical import (
    CanonicalSerializationError,
    canonical_from_json,
    canonical_json,
)
from sigilicon.layout.physical_design import (
    PhysicalDesignJob,
    PhysicalDesignResult,
)


def physical_design_job_from_json(text: str) -> PhysicalDesignJob:
    return canonical_from_json(text, PhysicalDesignJob)


def physical_design_result_from_json(text: str) -> PhysicalDesignResult:
    return canonical_from_json(text, PhysicalDesignResult)


def physical_design_job_id(job: PhysicalDesignJob) -> str:
    stable_job = PhysicalDesignJob(
        technology=job.technology,
        design=job.design,
        constraints=job.constraints,
        request=job.request,
        routing_constraints=job.routing_constraints,
    )
    digest = sha256(canonical_json(stable_job).encode("utf-8")).hexdigest()
    return (
        f"physical-design-job:{job.technology.name}:{job.design.name}:"
        f"sha256:{digest}"
    )


def physical_design_result_id(result: PhysicalDesignResult) -> str:
    return result.artifact_id


__all__ = [
    "CanonicalSerializationError",
    "canonical_json",
    "physical_design_job_from_json",
    "physical_design_job_id",
    "physical_design_result_from_json",
    "physical_design_result_id",
]
