"""Public physical-design execution Interface."""

from __future__ import annotations

from dataclasses import replace

from sigilicon.experimental.reference_pnr._closure import (
    PlacementRoutingClosureResult,
    close_placement_routing,
)
from sigilicon.experimental.reference_pnr._constraints import validate_constraint
from sigilicon.layout.physical_geometry import placed_sized_rect
from sigilicon.experimental.reference_pnr._objectives import validate_objective
from sigilicon.experimental.reference_pnr._placement import solve_placement
from sigilicon.experimental.reference_pnr._routing_check import check_routing_solution
from sigilicon.experimental.reference_pnr._routing_constraints import (
    evaluate_routing_constraints,
    validate_routing_constraint,
)
from sigilicon.experimental.reference_pnr._technology import (
    technology_capabilities,
    validate_technology,
)
from sigilicon.layout.physical_design import (
    ConstraintOutcome,
    ConstraintStatus,
    Diagnostic,
    LayerKind,
    Orientation,
    PhysicalDesignProvenance,
    PhysicalDesignStage,
    Rect,
    ResultStatus,
    RoutingBlockagePlacement,
    StageReport,
    TechnologyCapability,
)
from sigilicon.experimental.reference_pnr.model import (
    ReferencePnrJob,
    ReferencePnrResult,
    PhysicalOwnerSummary,
    PlacementRoutingClosureEvidence,
    PlacementRoutingRepairSummary,
    PlacementRoutingTerminationReason,
    ReferencePnrExecutionPolicy,
    RoutingConflictSummary,
    RoutingPlacementPressureSummary,
    RoutingTerminationReason,
)
from sigilicon.experimental.reference_pnr.serialization import reference_pnr_job_id
from sigilicon.layout.physical_design_serialization import physical_design_job_id


ENGINE_NAME = "sigilicon.reference_pnr"


class PnrInputError(ValueError):
    """The normalized physical-design job violates its structural contract."""


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return tuple(sorted(duplicate))


def _on_grid(value: int, grid: int) -> bool:
    return value % grid == 0


def _validate_job(job: ReferencePnrJob) -> None:
    if not isinstance(job, ReferencePnrJob):
        raise PnrInputError("job must be a ReferencePnrJob")
    if not isinstance(job.execution_policy, ReferencePnrExecutionPolicy):
        raise PnrInputError("job.execution_policy must be a ReferencePnrExecutionPolicy")
    errors: list[str] = []
    technology = job.technology
    design = job.design
    grid = technology.manufacturing_grid_dbu
    errors.extend(validate_technology(technology))
    if grid <= 0:
        grid = 1
    if not design.name:
        errors.append("design.name must be non-empty")
    if not job.request.stages:
        errors.append("request.stages must be non-empty")
    if any(not isinstance(stage, PhysicalDesignStage) for stage in job.request.stages):
        errors.append("request.stages must contain PhysicalDesignStage values")
    if len(set(job.request.stages)) != len(job.request.stages):
        errors.append("request.stages contains duplicates")
    spacing = job.request.minimum_instance_spacing_dbu
    if spacing < 0 or not _on_grid(spacing, grid):
        errors.append("minimum instance spacing must be non-negative and on-grid")
    if job.execution_policy.maximum_search_states <= 0:
        errors.append("maximum search states must be positive")
    if job.execution_policy.maximum_route_states <= 0:
        errors.append("maximum route states must be positive")
    if job.execution_policy.maximum_routing_iterations <= 0:
        errors.append("maximum routing iterations must be positive")
    if job.execution_policy.maximum_placement_repair_states <= 0:
        errors.append("maximum placement repair states must be positive")
    if job.execution_policy.maximum_placement_repair_iterations < 0:
        errors.append("maximum placement repair iterations must be non-negative")
    if (
        job.execution_policy.routing_congestion_bins_x <= 0
        or job.execution_policy.routing_congestion_bins_y <= 0
    ):
        errors.append("routing congestion bin counts must be positive")
    required_capabilities = job.request.required_technology_capabilities
    if any(
        not isinstance(capability, TechnologyCapability)
        for capability in required_capabilities
    ):
        errors.append(
            "required technology capabilities must contain TechnologyCapability values"
        )
    if len(set(required_capabilities)) != len(required_capabilities):
        errors.append("required technology capabilities contain duplicates")
    if PhysicalDesignStage.ROUTING in job.request.stages:
        if PhysicalDesignStage.PLACEMENT not in job.request.stages:
            errors.append("routing requires placement in request.stages")
        elif job.request.stages.index(PhysicalDesignStage.ROUTING) < job.request.stages.index(
            PhysicalDesignStage.PLACEMENT
        ):
            errors.append("placement must precede routing in request.stages")
    elif job.routing_constraints:
        errors.append("routing constraints require the routing stage")

    die_coordinates = (
        design.die.x_min,
        design.die.y_min,
        design.die.x_max,
        design.die.y_max,
    )
    if any(not _on_grid(value, grid) for value in die_coordinates):
        errors.append("design die must be aligned to the manufacturing grid")

    layers = {layer.name: layer for layer in technology.layers}

    master_names = tuple(master.name for master in design.masters)
    duplicates = _duplicates(master_names)
    if duplicates:
        errors.append(f"duplicate physical masters: {', '.join(duplicates)}")
    masters = {master.name: master for master in design.masters}
    for master in design.masters:
        if not master.name:
            errors.append("physical master names must be non-empty")
        if (
            master.width_dbu <= 0
            or master.height_dbu <= 0
            or not _on_grid(master.width_dbu, grid)
            or not _on_grid(master.height_dbu, grid)
        ):
            errors.append(f"master {master.name} dimensions must be positive and on-grid")
        if not master.allowed_orientations:
            errors.append(f"master {master.name} needs an allowed orientation")
        if any(
            not isinstance(orientation, Orientation)
            for orientation in master.allowed_orientations
        ):
            errors.append(f"master {master.name} has an invalid orientation")
        if len(set(master.allowed_orientations)) != len(master.allowed_orientations):
            errors.append(f"master {master.name} repeats an allowed orientation")
        pin_names = tuple(pin.name for pin in master.pins)
        pin_duplicates = _duplicates(pin_names)
        if pin_duplicates:
            errors.append(f"master {master.name} has duplicate pins: {', '.join(pin_duplicates)}")
        master_box = None
        if master.width_dbu > 0 and master.height_dbu > 0:
            master_box = Rect(0, 0, master.width_dbu, master.height_dbu)
        for pin in master.pins:
            if not pin.name:
                errors.append(f"master {master.name} has an empty pin name")
            for access in pin.accesses:
                if access.layer not in layers:
                    errors.append(
                        f"master {master.name} pin {pin.name} uses unknown layer {access.layer}"
                    )
                access_coordinates = (
                    access.shape.x_min,
                    access.shape.y_min,
                    access.shape.x_max,
                    access.shape.y_max,
                )
                if any(not _on_grid(value, grid) for value in access_coordinates):
                    errors.append(f"master {master.name} pin {pin.name} access is off-grid")
                if master_box is not None and not master_box.contains(access.shape):
                    errors.append(
                        f"master {master.name} pin {pin.name} access is outside the master"
                    )
        for obstruction in master.obstructions:
            if obstruction.layer not in layers:
                errors.append(
                    f"master {master.name} obstruction uses unknown layer "
                    f"{obstruction.layer}"
                )
            obstruction_coordinates = (
                obstruction.shape.x_min,
                obstruction.shape.y_min,
                obstruction.shape.x_max,
                obstruction.shape.y_max,
            )
            if any(not _on_grid(value, grid) for value in obstruction_coordinates):
                errors.append(f"master {master.name} obstruction is off-grid")
            if master_box is not None and not master_box.contains(obstruction.shape):
                errors.append(f"master {master.name} obstruction is outside the master")

    instance_names = tuple(instance.name for instance in design.instances)
    duplicates = _duplicates(instance_names)
    if duplicates:
        errors.append(f"duplicate physical instances: {', '.join(duplicates)}")
    instances = {instance.name: instance for instance in design.instances}
    for instance in design.instances:
        if not instance.name:
            errors.append("physical instance names must be non-empty")
        if instance.master not in masters:
            errors.append(f"instance {instance.name} uses unknown master {instance.master}")
            continue
        placement = instance.fixed_placement
        if placement is None:
            continue
        if placement.orientation not in masters[instance.master].allowed_orientations:
            errors.append(f"instance {instance.name} has a disallowed orientation")
        if not _on_grid(placement.origin.x, grid) or not _on_grid(placement.origin.y, grid):
            errors.append(f"instance {instance.name} fixed placement is off-grid")

    blockage_names = tuple(blockage.name for blockage in design.routing_blockages)
    duplicates = _duplicates(blockage_names)
    if duplicates:
        errors.append(f"duplicate routing blockages: {', '.join(duplicates)}")
    for blockage in design.routing_blockages:
        if not blockage.name:
            errors.append("routing blockage names must be non-empty")
        if (
            blockage.width_dbu <= 0
            or blockage.height_dbu <= 0
            or not _on_grid(blockage.width_dbu, grid)
            or not _on_grid(blockage.height_dbu, grid)
        ):
            errors.append(
                f"routing blockage {blockage.name} dimensions must be positive and on-grid"
            )
        if not blockage.shapes:
            errors.append(f"routing blockage {blockage.name} needs at least one shape")
        if not blockage.allowed_orientations:
            errors.append(
                f"routing blockage {blockage.name} needs an allowed orientation"
            )
        if any(
            not isinstance(orientation, Orientation)
            for orientation in blockage.allowed_orientations
        ):
            errors.append(f"routing blockage {blockage.name} has an invalid orientation")
        if len(set(blockage.allowed_orientations)) != len(
            blockage.allowed_orientations
        ):
            errors.append(
                f"routing blockage {blockage.name} repeats an allowed orientation"
            )
        if blockage.placement.orientation not in blockage.allowed_orientations:
            errors.append(
                f"routing blockage {blockage.name} has a disallowed orientation"
            )
        if not _on_grid(
            blockage.placement.origin.x,
            grid,
        ) or not _on_grid(blockage.placement.origin.y, grid):
            errors.append(f"routing blockage {blockage.name} placement is off-grid")
        local_box = None
        if blockage.width_dbu > 0 and blockage.height_dbu > 0:
            local_box = Rect(0, 0, blockage.width_dbu, blockage.height_dbu)
        for shape in blockage.shapes:
            if shape.layer not in layers:
                errors.append(
                    f"routing blockage {blockage.name} uses unknown layer {shape.layer}"
                )
            coordinates = (
                shape.shape.x_min,
                shape.shape.y_min,
                shape.shape.x_max,
                shape.shape.y_max,
            )
            if any(not _on_grid(value, grid) for value in coordinates):
                errors.append(f"routing blockage {blockage.name} shape is off-grid")
            if local_box is not None and not local_box.contains(shape.shape):
                errors.append(
                    f"routing blockage {blockage.name} shape is outside its local bounds"
                )
        if local_box is not None:
            placed_box = placed_sized_rect(
                blockage.width_dbu,
                blockage.height_dbu,
                blockage.placement,
            )
            if not design.die.contains(placed_box):
                errors.append(
                    f"routing blockage {blockage.name} placement is outside the die"
                )
            if blockage.repair_region is not None and not blockage.repair_region.contains(
                placed_box
            ):
                errors.append(
                    f"routing blockage {blockage.name} placement is outside its repair region"
                )
        if blockage.repair_region is not None:
            coordinates = (
                blockage.repair_region.x_min,
                blockage.repair_region.y_min,
                blockage.repair_region.x_max,
                blockage.repair_region.y_max,
            )
            if any(not _on_grid(value, grid) for value in coordinates):
                errors.append(
                    f"routing blockage {blockage.name} repair region is off-grid"
                )
            if not design.die.contains(blockage.repair_region):
                errors.append(
                    f"routing blockage {blockage.name} repair region is outside the die"
                )

    port_names = tuple(port.name for port in design.ports)
    duplicates = _duplicates(port_names)
    if duplicates:
        errors.append(f"duplicate physical ports: {', '.join(duplicates)}")
    ports = {port.name: port for port in design.ports}
    for port in design.ports:
        if not port.name:
            errors.append("physical port names must be non-empty")
        for access in port.accesses:
            if access.layer not in layers:
                errors.append(f"port {port.name} uses unknown layer {access.layer}")
            access_coordinates = (
                access.shape.x_min,
                access.shape.y_min,
                access.shape.x_max,
                access.shape.y_max,
            )
            if any(not _on_grid(value, grid) for value in access_coordinates):
                errors.append(f"port {port.name} access is off-grid")
            if not design.die.contains(access.shape):
                errors.append(f"port {port.name} access is outside the die")

    net_names = tuple(net.name for net in design.nets)
    duplicates = _duplicates(net_names)
    if duplicates:
        errors.append(f"duplicate physical nets: {', '.join(duplicates)}")
    for net in design.nets:
        if not net.name:
            errors.append("physical net names must be non-empty")
        if len(net.pins) < 2:
            errors.append(f"net {net.name} must contain at least two pin references")
        if len(set(net.pins)) != len(net.pins):
            errors.append(f"net {net.name} repeats a pin reference")
        for reference in net.pins:
            if not reference.pin:
                errors.append(f"net {net.name} has an empty pin reference")
                continue
            if reference.instance is None:
                if reference.pin not in ports:
                    errors.append(f"net {net.name} uses unknown port {reference.pin}")
                continue
            instance = instances.get(reference.instance)
            if instance is None:
                errors.append(f"net {net.name} uses unknown instance {reference.instance}")
                continue
            master = masters.get(instance.master)
            if master is not None and reference.pin not in {pin.name for pin in master.pins}:
                errors.append(
                    f"net {net.name} uses unknown pin {reference.instance}.{reference.pin}"
                )

    routing_constraint_names = tuple(
        constraint.name for constraint in job.routing_constraints
    )
    duplicates = _duplicates(routing_constraint_names)
    if duplicates:
        errors.append(f"duplicate routing constraints: {', '.join(duplicates)}")
    shared_constraint_names = set(routing_constraint_names) & {
        constraint.name for constraint in job.constraints
    }
    if shared_constraint_names:
        errors.append(
            "constraint names must be unique across placement and routing: "
            + ", ".join(sorted(shared_constraint_names))
        )
    routing_layers = frozenset(
        name for name, layer in layers.items() if layer.kind is LayerKind.ROUTING
    )
    for constraint in job.routing_constraints:
        errors.extend(
            validate_routing_constraint(
                constraint,
                known_nets=frozenset(net_names),
                known_layers=routing_layers,
                grid=grid,
                die=design.die,
            )
        )

    constraint_names = tuple(constraint.name for constraint in job.constraints)
    duplicates = _duplicates(constraint_names)
    if duplicates:
        errors.append(f"duplicate placement constraints: {', '.join(duplicates)}")
    for constraint in job.constraints:
        errors.extend(
            validate_constraint(
                constraint,
                known_instances=frozenset(instances),
                grid=grid,
            )
        )

    objective_names = tuple(objective.name for objective in job.request.objectives)
    duplicates = _duplicates(objective_names)
    if duplicates:
        errors.append(f"duplicate placement objectives: {', '.join(duplicates)}")
    for objective in job.request.objectives:
        errors.extend(validate_objective(objective, job=job))

    if errors:
        raise PnrInputError("invalid physical-design job: " + "; ".join(errors))


def _provenance(job: ReferencePnrJob) -> PhysicalDesignProvenance:
    return PhysicalDesignProvenance(
        backend=ENGINE_NAME,
        job_identity=physical_design_job_id(job),
        deterministic=True,
    )


def _routing_constraints_not_evaluated(
    job: ReferencePnrJob,
) -> tuple[ConstraintOutcome, ...]:
    return tuple(
        ConstraintOutcome(
            constraint=constraint.name,
            status=ConstraintStatus.NOT_EVALUATED,
            message="routing constraint was not evaluated",
        )
        for constraint in job.routing_constraints
    )


def _initial_routing_blockage_placements(
    job: ReferencePnrJob,
) -> tuple[RoutingBlockagePlacement, ...]:
    return tuple(
        RoutingBlockagePlacement(blockage.name, blockage.placement)
        for blockage in sorted(
            job.design.routing_blockages,
            key=lambda item: item.name,
        )
    )


def _public_owner_summary(owner) -> PhysicalOwnerSummary:
    return PhysicalOwnerSummary(
        identity=owner.identity,
        mobility=owner.mobility,
        repair_owner=owner.repair_owner,
    )


def _public_conflict_summary(conflict) -> RoutingConflictSummary:
    return RoutingConflictSummary(
        identity=conflict.identity,
        kind=conflict.kind,
        resource=(
            None if conflict.resource is None else conflict.resource.stable_name
        ),
        aggressor_nets=conflict.aggressor_nets,
        occupant_nets=conflict.occupant_nets,
        affected_group=conflict.affected_group,
        severity=conflict.severity,
        cost=conflict.cost,
        evidence=conflict.evidence,
        physical_owners=conflict.physical_owners,
        resource_overflow=conflict.resource_overflow,
    )


def _public_pressure_summary(site) -> RoutingPlacementPressureSummary:
    return RoutingPlacementPressureSummary(
        identity=site.identity,
        source_conflict=site.source_conflict,
        conflict_kind=site.conflict_kind,
        resource=None if site.resource is None else site.resource.stable_name,
        region=site.region,
        physical_owner_candidates=tuple(
            _public_owner_summary(owner)
            for owner in site.physical_owner_candidates
        ),
        involved_nets=site.involved_nets,
        involved_groups=site.involved_groups,
        severity=site.severity,
        cost=site.cost,
        evidence=site.evidence,
        reason=site.reason,
        repair_scope=site.repair_scope,
    )


def _public_closure_evidence(
    closure: PlacementRoutingClosureResult,
    artifact_id: str,
) -> PlacementRoutingClosureEvidence:
    return PlacementRoutingClosureEvidence(
        termination=closure.termination,
        routing_termination=closure.routing.termination.reason,
        quality=closure.quality,
        conflict_identities=tuple(
            conflict.identity for conflict in closure.routing.conflicts.conflicts
        ),
        pressure_identities=tuple(
            site.identity for site in closure.routing.placement_pressure.sites
        ),
        conflicts=tuple(
            _public_conflict_summary(conflict)
            for conflict in closure.routing.conflicts.conflicts
        ),
        placement_pressure=tuple(
            _public_pressure_summary(site)
            for site in closure.routing.placement_pressure.sites
        ),
        repairs=tuple(
            PlacementRoutingRepairSummary(
                iteration=repair.iteration,
                moved_owner=repair.moved_owner,
                attributed_owners=repair.attributed_owners,
                source_conflicts=repair.source_conflicts,
                source_pressures=repair.source_pressures,
                source_pressure=tuple(
                    _public_pressure_summary(site)
                    for site in repair.source_pressure
                ),
                displacement_dbu=repair.displacement_dbu,
                predicted_released_resources=tuple(
                    resource.stable_name
                    for resource in repair.predicted_released_resources
                ),
                predicted_released_pressure=(
                    repair.predicted_released_pressure
                ),
                predicted_remaining_pressure=(
                    repair.predicted_remaining_pressure
                ),
                predicted_pin_access_gain=repair.predicted_pin_access_gain,
                predicted_pin_access_loss=repair.predicted_pin_access_loss,
                current_quality=repair.current_quality,
                candidate_quality=repair.candidate_quality,
                decision=repair.quality_decision,
                accepted=repair.accepted,
            )
            for repair in closure.repairs
        ),
        artifact_id=artifact_id,
    )


def _closure_is_closed(closure: PlacementRoutingClosureResult) -> bool:
    return (
        closure.termination is PlacementRoutingTerminationReason.CLOSED
        and closure.routing.termination.reason is RoutingTerminationReason.CLOSED
        and closure.quality.closed
    )


def run(job: ReferencePnrJob) -> ReferencePnrResult:
    """Solve a normalized physical-design job without external side effects.

    Structurally invalid jobs raise :class:`PnrInputError`. Valid jobs that are
    infeasible or request unsupported capabilities return an explicit result.
    """

    _validate_job(job)
    result_identity = f"{reference_pnr_job_id(job)}:result"
    closure_evidence_identity = f"{result_identity}:closure-evidence"
    capabilities = technology_capabilities(job.technology)
    missing_capabilities = tuple(
        capability
        for capability in job.request.required_technology_capabilities
        if capability not in capabilities
    )
    if missing_capabilities:
        reports: list[StageReport] = []
        diagnostic = Diagnostic(
            code="unsupported_technology_capability",
            message="technology model does not provide required capabilities",
            entities=tuple(capability.value for capability in missing_capabilities),
        )
        reports.append(
            StageReport(
                stage=job.request.stages[0],
                status=ResultStatus.UNSUPPORTED,
                diagnostics=(diagnostic,),
            )
        )
        return ReferencePnrResult(
            status=ResultStatus.UNSUPPORTED,
            placements=(),
            constraint_outcomes=tuple(
                ConstraintOutcome(
                    constraint=constraint.name,
                    status=ConstraintStatus.NOT_EVALUATED,
                    message="constraint was not evaluated",
                )
                for constraint in job.constraints
            )
            + _routing_constraints_not_evaluated(job),
            stage_reports=tuple(reports),
            provenance=_provenance(job),
            routing_blockage_placements=_initial_routing_blockage_placements(job),
            artifact_id=result_identity,
        )

    placement = solve_placement(job)
    if placement.status is not ResultStatus.SUCCEEDED:
        return ReferencePnrResult(
            status=placement.status,
            placements=placement.placements,
            constraint_outcomes=(
                placement.constraint_outcomes
                + _routing_constraints_not_evaluated(job)
            ),
            stage_reports=(placement.report,),
            provenance=_provenance(job),
            routing_blockage_placements=_initial_routing_blockage_placements(job),
            artifact_id=result_identity,
        )
    if PhysicalDesignStage.ROUTING in job.request.stages:
        closure = close_placement_routing(job, placement)
        placement = closure.placement
        routing = closure.routing
        if routing.status is ResultStatus.SUCCEEDED:
            routing_diagnostics = check_routing_solution(
                job,
                placement.placements,
                routing.routes,
                closure.routing_blockage_placements,
            )
            routing_outcomes = evaluate_routing_constraints(
                job,
                routing.routes,
                placement.placements,
            )
            violated_routing_constraints = tuple(
                outcome
                for outcome in routing_outcomes
                if outcome.status is ConstraintStatus.VIOLATED
            )
            if routing_diagnostics or violated_routing_constraints:
                constraint_diagnostics = tuple(
                    Diagnostic(
                        code="routing_constraint_violated",
                        message=outcome.message,
                        entities=(outcome.constraint,),
                    )
                    for outcome in violated_routing_constraints
                )
                return ReferencePnrResult(
                    status=ResultStatus.FAILED,
                    placements=placement.placements,
                    constraint_outcomes=(
                        placement.constraint_outcomes + routing_outcomes
                    ),
                    stage_reports=(
                        placement.report,
                        replace(
                            routing.report,
                            status=ResultStatus.FAILED,
                            diagnostics=(
                                routing_diagnostics + constraint_diagnostics
                            ),
                        ),
                    ),
                    provenance=_provenance(job),
                    routes=routing.routes,
                    routing_blockage_placements=(
                        closure.routing_blockage_placements
                    ),
                    closure_evidence=_public_closure_evidence(
                        closure, closure_evidence_identity
                    ),
                    closed=False,
                    artifact_id=result_identity,
                )
        return ReferencePnrResult(
            status=routing.status,
            placements=placement.placements,
            constraint_outcomes=(
                placement.constraint_outcomes
                + (
                    evaluate_routing_constraints(
                        job,
                        routing.routes,
                        placement.placements,
                    )
                    if routing.status is ResultStatus.SUCCEEDED
                    else _routing_constraints_not_evaluated(job)
                )
            ),
            stage_reports=(placement.report, routing.report),
            provenance=_provenance(job),
            routes=routing.routes,
            routing_blockage_placements=closure.routing_blockage_placements,
            closure_evidence=_public_closure_evidence(
                closure, closure_evidence_identity
            ),
            closed=_closure_is_closed(closure),
            artifact_id=result_identity,
        )
    return ReferencePnrResult(
        status=placement.status,
        placements=placement.placements,
        constraint_outcomes=placement.constraint_outcomes,
        stage_reports=(placement.report,),
        provenance=_provenance(job),
        routing_blockage_placements=_initial_routing_blockage_placements(job),
        closed=True,
        artifact_id=result_identity,
    )
