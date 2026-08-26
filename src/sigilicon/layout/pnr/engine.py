"""Public physical-design execution Interface."""

from __future__ import annotations

from sigilicon.layout.pnr._constraints import validate_constraint
from sigilicon.layout.pnr._objectives import validate_objective
from sigilicon.layout.pnr._placement import solve_placement
from sigilicon.layout.pnr._serialization import canonical_sha256
from sigilicon.layout.pnr.model import (
    ConstraintOutcome,
    ConstraintStatus,
    Diagnostic,
    LayerKind,
    Orientation,
    PhysicalDesignJob,
    PhysicalDesignResult,
    PnrProvenance,
    PnrStage,
    Rect,
    ResultStatus,
    RoutingDirection,
    StageReport,
)


ENGINE_NAME = "sigilicon.reference_pnr"
ENGINE_VERSION = 3
ALGORITHM = "deterministic_weighted_search_v1"


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


def _validate_job(job: PhysicalDesignJob) -> None:
    if not isinstance(job, PhysicalDesignJob):
        raise PnrInputError("job must be a PhysicalDesignJob")
    errors: list[str] = []
    technology = job.technology
    design = job.design
    grid = technology.manufacturing_grid_dbu
    if not technology.name:
        errors.append("technology.name must be non-empty")
    if technology.dbu_per_micron <= 0:
        errors.append("technology.dbu_per_micron must be positive")
    if grid <= 0:
        errors.append("technology.manufacturing_grid_dbu must be positive")
        grid = 1
    if not design.name:
        errors.append("design.name must be non-empty")
    if not job.request.stages:
        errors.append("request.stages must be non-empty")
    if any(not isinstance(stage, PnrStage) for stage in job.request.stages):
        errors.append("request.stages must contain PnrStage values")
    if len(set(job.request.stages)) != len(job.request.stages):
        errors.append("request.stages contains duplicates")
    spacing = job.request.minimum_instance_spacing_dbu
    if spacing < 0 or not _on_grid(spacing, grid):
        errors.append("minimum instance spacing must be non-negative and on-grid")
    if job.request.maximum_search_states <= 0:
        errors.append("maximum search states must be positive")

    die_coordinates = (
        design.die.x_min,
        design.die.y_min,
        design.die.x_max,
        design.die.y_max,
    )
    if any(not _on_grid(value, grid) for value in die_coordinates):
        errors.append("design die must be aligned to the manufacturing grid")

    layer_names = tuple(layer.name for layer in technology.layers)
    duplicates = _duplicates(layer_names)
    if duplicates:
        errors.append(f"duplicate physical layers: {', '.join(duplicates)}")
    layers = {layer.name: layer for layer in technology.layers}
    for layer in technology.layers:
        if not layer.name:
            errors.append("physical layer names must be non-empty")
        if not isinstance(layer.kind, LayerKind):
            errors.append(f"physical layer {layer.name} has an invalid kind")
        if layer.direction is not None and not isinstance(
            layer.direction, RoutingDirection
        ):
            errors.append(f"physical layer {layer.name} has an invalid direction")
        if layer.kind is LayerKind.ROUTING and layer.direction is None:
            errors.append(f"routing layer {layer.name} needs a direction")
        if layer.kind is not LayerKind.ROUTING and layer.direction not in {
            None,
            RoutingDirection.ANY,
        }:
            errors.append(f"non-routing layer {layer.name} has a routing direction")
        for field_name, value in (
            ("minimum_width_dbu", layer.minimum_width_dbu),
            ("minimum_spacing_dbu", layer.minimum_spacing_dbu),
        ):
            if value is not None and (value <= 0 or not _on_grid(value, grid)):
                errors.append(f"layer {layer.name} {field_name} must be positive and on-grid")

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


def _provenance(job: PhysicalDesignJob) -> PnrProvenance:
    return PnrProvenance(
        engine=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        algorithm=ALGORITHM,
        input_sha256=canonical_sha256(job),
        deterministic=True,
    )


def run(job: PhysicalDesignJob) -> PhysicalDesignResult:
    """Solve a normalized physical-design job without external side effects.

    Structurally invalid jobs raise :class:`PnrInputError`. Valid jobs that are
    infeasible or request unsupported capabilities return an explicit result.
    """

    _validate_job(job)
    unsupported_stages = tuple(
        stage for stage in job.request.stages if stage is not PnrStage.PLACEMENT
    )
    if unsupported_stages:
        reports: list[StageReport] = []
        for stage in unsupported_stages:
            diagnostic = Diagnostic(
                code="unsupported_stage",
                message=f"reference engine does not implement {stage.value}",
                entities=(stage.value,),
            )
            reports.append(
                StageReport(
                    stage=stage,
                    status=ResultStatus.UNSUPPORTED,
                    diagnostics=(diagnostic,),
                )
            )
        return PhysicalDesignResult(
            status=ResultStatus.UNSUPPORTED,
            placements=(),
            constraint_outcomes=tuple(
                ConstraintOutcome(
                    constraint=constraint.name,
                    status=ConstraintStatus.NOT_EVALUATED,
                    message="constraint was not evaluated",
                )
                for constraint in job.constraints
            ),
            stage_reports=tuple(reports),
            provenance=_provenance(job),
        )

    placement = solve_placement(job)
    return PhysicalDesignResult(
        status=placement.status,
        placements=placement.placements,
        constraint_outcomes=placement.constraint_outcomes,
        stage_reports=(placement.report,),
        provenance=_provenance(job),
    )
