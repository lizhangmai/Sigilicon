"""Deterministic Placement↔Routing closure over typed pressure and repair."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from sigilicon.layout.pnr._constraints import constraint_outcomes
from sigilicon.layout.pnr._legality import placed_rect
from sigilicon.layout.pnr._placement import PlacementSolveResult, _placement_score
from sigilicon.layout.pnr._placement_repair import (
    PlacementIdentity,
    PlacementRepairStatus,
    compile_placement_repair_problem,
)
from sigilicon.layout.pnr._routing_ownership import PhysicalOwnerIdentity
from sigilicon.layout.pnr._routing_resources import RoutingResourceIdentity
from sigilicon.layout.pnr._routing import RoutingSolveResult, solve_routing
from sigilicon.layout.pnr._routing_conflicts import RoutingTerminationReason
from sigilicon.layout.pnr._routing_quality import (
    RoutingClosureQuality,
    RoutingClosureQualityDecision,
    RoutingClosureQualityPolicy,
    compile_routing_closure_quality,
)
from sigilicon.layout.pnr.model import (
    Diagnostic,
    Metric,
    PhysicalDesignJob,
    ResultStatus,
    StageReport,
)


class PlacementRoutingTerminationReason(str, Enum):
    CLOSED = "closed"
    ROUTING_TERMINATED = "routing_terminated"
    NO_LEGAL_REPAIR = "no_legal_repair"
    REPAIR_STATE_BUDGET = "repair_state_budget"
    REPAIR_ITERATION_BUDGET = "repair_iteration_budget"
    INDEPENDENT_EVALUATION_FAILED = "independent_evaluation_failed"


@dataclass(frozen=True)
class PlacementRoutingRepairEvidence:
    iteration: int
    placement: PlacementIdentity
    moved_instance: str
    attributed_owners: tuple[PhysicalOwnerIdentity, ...]
    displacement_dbu: int
    predicted_released_resources: tuple[RoutingResourceIdentity, ...]
    predicted_released_pressure: int
    predicted_remaining_pressure: int
    predicted_pin_access_gain: int
    predicted_pin_access_loss: int
    routed_net_improvement: int
    conflict_reduction: int
    current_quality: RoutingClosureQuality
    candidate_quality: RoutingClosureQuality
    quality_decision: RoutingClosureQualityDecision
    accepted: bool


@dataclass(frozen=True)
class PlacementRoutingClosureResult:
    status: ResultStatus
    placement: PlacementSolveResult
    routing: RoutingSolveResult
    quality: RoutingClosureQuality
    repairs: tuple[PlacementRoutingRepairEvidence, ...]
    termination: PlacementRoutingTerminationReason


def _final_placement(
    job: PhysicalDesignJob,
    initial: PlacementSolveResult,
    placements,
    repairs: tuple[PlacementRoutingRepairEvidence, ...],
) -> PlacementSolveResult:
    placement_map = {item.instance: item.placement for item in placements}
    masters = {master.name: master for master in job.design.masters}
    instances = {instance.name: instance for instance in job.design.instances}
    rectangles = {
        name: placed_rect(masters[instances[name].master], placement)
        for name, placement in placement_map.items()
    }
    accepted = tuple(item for item in repairs if item.accepted)
    total_score, soft_penalty, objective_values = _placement_score(
        job,
        rectangles,
        placement_map,
    )
    names = {
        "placement_repair_iterations",
        "placement_repair_accepted_count",
        "placement_repair_displacement",
        "placement_routing_net_improvement",
        "placement_routing_conflict_reduction",
        "placement_score",
        "soft_constraint_penalty",
    }
    metrics = tuple(
        metric
        for metric in initial.report.metrics
        if metric.name not in names and not metric.name.startswith("objective.")
    ) + (
        Metric("placement_score", total_score, "score"),
        Metric("soft_constraint_penalty", soft_penalty, "score"),
        *(
            Metric(f"objective.{name}", value, unit)
            for name, value, unit in objective_values
        ),
        Metric("placement_repair_iterations", len(repairs), "count"),
        Metric("placement_repair_accepted_count", len(accepted), "count"),
        Metric(
            "placement_repair_displacement",
            sum(item.displacement_dbu for item in accepted),
            "dbu",
        ),
        Metric(
            "placement_routing_net_improvement",
            sum(item.routed_net_improvement for item in accepted),
            "count",
        ),
        Metric(
            "placement_routing_conflict_reduction",
            sum(item.conflict_reduction for item in accepted),
            "count",
        ),
    )
    return PlacementSolveResult(
        initial.status,
        placements,
        constraint_outcomes(job.constraints, rectangles, placement_map),
        replace(initial.report, metrics=metrics),
    )


def _closure_result(
    job: PhysicalDesignJob,
    initial: PlacementSolveResult,
    placements,
    routing: RoutingSolveResult,
    repairs: tuple[PlacementRoutingRepairEvidence, ...],
    termination: PlacementRoutingTerminationReason,
    *,
    status: ResultStatus | None = None,
    diagnostic: Diagnostic | None = None,
) -> PlacementRoutingClosureResult:
    effective_status = routing.status if status is None else status
    effective_routing = routing
    if diagnostic is not None or effective_status is not routing.status:
        report = StageReport(
            routing.report.stage,
            effective_status,
            routing.report.diagnostics
            + (() if diagnostic is None else (diagnostic,)),
            routing.report.metrics,
        )
        effective_routing = replace(
            routing,
            status=effective_status,
            report=report,
        )
    return PlacementRoutingClosureResult(
        effective_status,
        _final_placement(job, initial, placements, repairs),
        effective_routing,
        compile_routing_closure_quality(
            job,
            effective_routing,
            placements,
            initial_placements=initial.placements,
        ),
        repairs,
        termination,
    )


def close_placement_routing(
    job: PhysicalDesignJob,
    initial: PlacementSolveResult,
) -> PlacementRoutingClosureResult:
    """Repair only pressure-attributed placement and recompile every route trial."""

    placements = initial.placements
    routing = solve_routing(job, placements)
    quality = compile_routing_closure_quality(
        job,
        routing,
        placements,
        initial_placements=initial.placements,
    )
    quality_policy = RoutingClosureQualityPolicy()
    if quality.closed:
        return _closure_result(
            job,
            initial,
            placements,
            routing,
            (),
            PlacementRoutingTerminationReason.CLOSED,
        )
    if routing.status is ResultStatus.SUCCEEDED:
        return _closure_result(
            job,
            initial,
            placements,
            routing,
            (),
            PlacementRoutingTerminationReason.INDEPENDENT_EVALUATION_FAILED,
        )

    budget = job.execution_policy.maximum_placement_repair_iterations
    if budget == 0 or (
        routing.termination.reason is RoutingTerminationReason.STATE_BUDGET
    ):
        return _closure_result(
            job,
            initial,
            placements,
            routing,
            (),
            PlacementRoutingTerminationReason.ROUTING_TERMINATED,
        )
    if (
        routing.termination.reason is RoutingTerminationReason.UNSUPPORTED
        and not routing.placement_pressure.movable_instances
    ):
        return _closure_result(
            job,
            initial,
            placements,
            routing,
            (),
            PlacementRoutingTerminationReason.ROUTING_TERMINATED,
        )
    if not routing.placement_pressure.movable_instances:
        return _closure_result(
            job,
            initial,
            placements,
            routing,
            (),
            PlacementRoutingTerminationReason.NO_LEGAL_REPAIR,
        )

    rejected: set[PlacementIdentity] = set()
    repairs: list[PlacementRoutingRepairEvidence] = []
    for iteration in range(1, budget + 1):
        repair_problem = compile_placement_repair_problem(
            job,
            placements,
            routing.placement_pressure,
            rejected=frozenset(rejected),
        )
        repair = repair_problem.next_candidate()
        if repair.status is not PlacementRepairStatus.REPAIRED:
            termination = (
                PlacementRoutingTerminationReason.REPAIR_STATE_BUDGET
                if repair.status is PlacementRepairStatus.STATE_BUDGET
                else PlacementRoutingTerminationReason.NO_LEGAL_REPAIR
            )
            return _closure_result(
                job,
                initial,
                placements,
                routing,
                tuple(repairs),
                termination,
                diagnostic=Diagnostic(
                    "placement_repair_unavailable",
                    repair.reason,
                    routing.placement_pressure.movable_instances,
                ),
            )
        rejected.add(repair.identity)
        if repair.prediction is None:
            raise RuntimeError("repair candidate is missing typed prediction")
        candidate = solve_routing(job, repair.placements)
        candidate_quality = compile_routing_closure_quality(
            job,
            candidate,
            repair.placements,
            initial_placements=initial.placements,
        )
        routed_improvement = len(candidate.routes) - len(routing.routes)
        conflict_reduction = (
            len(routing.conflicts.conflicts)
            - len(candidate.conflicts.conflicts)
        )
        decision = quality_policy.compare(candidate_quality, quality)
        accepted = decision is RoutingClosureQualityDecision.IMPROVED
        repairs.append(
            PlacementRoutingRepairEvidence(
                iteration,
                repair.identity,
                repair.moved_instance or "",
                repair.attributed_owners,
                repair.displacement_dbu,
                repair.prediction.released_resources,
                repair.prediction.released_pressure,
                repair.prediction.remaining_pressure,
                repair.prediction.pin_access_gain,
                repair.prediction.pin_access_loss,
                routed_improvement,
                conflict_reduction,
                quality,
                candidate_quality,
                decision,
                accepted,
            )
        )
        if not accepted:
            continue
        placements = repair.placements
        routing = candidate
        quality = candidate_quality
        if quality.closed:
            return _closure_result(
                job,
                initial,
                placements,
                routing,
                tuple(repairs),
                PlacementRoutingTerminationReason.CLOSED,
            )

    return _closure_result(
        job,
        initial,
        placements,
        routing,
        tuple(repairs),
        PlacementRoutingTerminationReason.REPAIR_ITERATION_BUDGET,
        status=ResultStatus.EXHAUSTED,
        diagnostic=Diagnostic(
            "placement_repair_iteration_exhausted",
            "placement and routing did not close within the repair iteration budget",
            routing.placement_pressure.movable_instances,
        ),
    )
