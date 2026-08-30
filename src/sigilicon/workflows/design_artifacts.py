"""Application workflow for complete front-end artifact chains."""

from __future__ import annotations

from sigilicon.domain.circuit_design import (
    CandidateValidation,
    DesignCandidate,
    design_artifact_from_json,
    validate_design_candidate,
)


def validate_candidate_records(
    candidate_json: str,
    artifact_json: tuple[str, ...],
    *,
    expected_owner: str | None = None,
) -> CandidateValidation:
    """Validate one exact Candidate record and all of its referenced records."""

    candidate = design_artifact_from_json(candidate_json)
    if not isinstance(candidate, DesignCandidate):
        raise ValueError("candidate input is not a Design Candidate")
    if expected_owner is not None and candidate.metadata.owner != expected_owner:
        raise ValueError("Candidate owner does not match the selected project owner")
    artifacts = tuple(design_artifact_from_json(text) for text in artifact_json)
    return validate_design_candidate(candidate, artifacts)


__all__ = ["validate_candidate_records"]
