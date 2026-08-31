"""Experimental in-process placement-and-routing reference implementation."""

from sigilicon.experimental.reference_pnr.engine import PnrInputError, run
from sigilicon.layout.physical_design import *  # noqa: F401,F403
from sigilicon.experimental.reference_pnr.model import (
    PhysicalOwnerMobility,
    PlacementRoutingClosureEvidence,
    PlacementRoutingRepairSummary,
    PlacementRoutingTerminationReason,
    ReferencePnrExecutionPolicy,
    ReferencePnrJob,
    ReferencePnrJobLineage,
    ReferencePnrResult,
    RoutingClosureQuality,
    RoutingClosureQualityDecision,
    RoutingConflictKind,
    RoutingConflictSummary,
    RoutingPlacementPressureSummary,
    RoutingTerminationReason,
)
from sigilicon.experimental.reference_pnr.serialization import (
    CanonicalSerializationError,
    canonical_json,
    physical_closure_evidence_id,
    placement_routing_closure_evidence_from_json,
    reference_pnr_job_from_json,
    reference_pnr_result_from_json,
)

__all__ = [
    "PnrInputError",
    "CanonicalSerializationError",
    "PhysicalOwnerMobility",
    "PlacementRoutingClosureEvidence",
    "PlacementRoutingRepairSummary",
    "PlacementRoutingTerminationReason",
    "ReferencePnrExecutionPolicy",
    "ReferencePnrJob",
    "ReferencePnrJobLineage",
    "ReferencePnrResult",
    "RoutingClosureQuality",
    "RoutingClosureQualityDecision",
    "RoutingConflictKind",
    "RoutingConflictSummary",
    "RoutingPlacementPressureSummary",
    "RoutingTerminationReason",
    "canonical_json",
    "physical_closure_evidence_id",
    "placement_routing_closure_evidence_from_json",
    "reference_pnr_job_from_json",
    "reference_pnr_result_from_json",
    "run",
]
