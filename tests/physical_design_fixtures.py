"""Typed physical-design fixtures independent of any bundled solver."""

from __future__ import annotations

from sigilicon.layout.physical_design import (
    ConstraintOutcome,
    GridlessRoutingResource,
    InstancePlacement,
    LayerKind,
    MinimumSpacingRule,
    MinimumWidthRule,
    NetRoute,
    PhysicalDesign,
    PhysicalDesignJob,
    PhysicalDesignProvenance,
    PhysicalDesignRequest,
    PhysicalDesignResult,
    PhysicalDesignStage,
    PhysicalLayer,
    PhysicalNet,
    PhysicalPort,
    PhysicalTechnology,
    PinAccess,
    PinReference,
    Point,
    Rect,
    ResultStatus,
    RouteSegment,
    RoutingBlockagePlacement,
    RoutingDirection,
    StageReport,
)
from sigilicon.layout.physical_design_serialization import physical_design_job_id


def routed_job(name: str = "contract-physical-design") -> PhysicalDesignJob:
    """Return a minimal stable Job with one legal gridless route."""

    technology = PhysicalTechnology(
        f"{name}-technology",
        dbu_per_micron=1000,
        manufacturing_grid_dbu=1,
        layers=(PhysicalLayer("route", LayerKind.ROUTING, RoutingDirection.ANY),),
        routing_resources=(GridlessRoutingResource("route-domain", "route"),),
        rules=(
            MinimumWidthRule("route-width", "route", 2),
            MinimumSpacingRule("route-spacing", "route", 1),
        ),
    )
    return PhysicalDesignJob(
        technology,
        PhysicalDesign(
            name,
            Rect(0, 0, 20, 10),
            (),
            (),
            ports=(
                PhysicalPort("source", (PinAccess("route", Rect(1, 1, 3, 3)),)),
                PhysicalPort("sink", (PinAccess("route", Rect(17, 1, 19, 3)),)),
            ),
            nets=(
                PhysicalNet(
                    "signal",
                    (PinReference("source"), PinReference("sink")),
                ),
            ),
        ),
        request=PhysicalDesignRequest(
            stages=(PhysicalDesignStage.PLACEMENT, PhysicalDesignStage.ROUTING)
        ),
    )


def typed_result(
    job: PhysicalDesignJob,
    *,
    status: ResultStatus = ResultStatus.SUCCEEDED,
    closed: bool | None = None,
    routes: tuple[NetRoute, ...] | None = None,
    backend: str = "contract-fixture",
) -> PhysicalDesignResult:
    """Return a stable Result whose provenance binds the supplied Job exactly."""

    if closed is None:
        closed = status is ResultStatus.SUCCEEDED
    if routes is None:
        routes = (
            (
                NetRoute(
                    "signal",
                    (
                        RouteSegment(
                            "signal",
                            "route",
                            Point(2, 2),
                            Point(18, 2),
                            2,
                        ),
                    ),
                ),
            )
            if status in {ResultStatus.SUCCEEDED, ResultStatus.EXHAUSTED}
            else ()
        )
    placements = tuple(
        InstancePlacement(instance.name, instance.fixed_placement)
        for instance in job.design.instances
        if instance.fixed_placement is not None
    )
    blockages = tuple(
        RoutingBlockagePlacement(blockage.name, blockage.placement)
        for blockage in job.design.routing_blockages
    )
    identity = physical_design_job_id(job)
    return PhysicalDesignResult(
        status=status,
        placements=placements,
        constraint_outcomes=(),
        stage_reports=tuple(
            StageReport(stage, status)
            for stage in job.request.stages
        ),
        provenance=PhysicalDesignProvenance(backend, identity, True),
        routes=routes,
        routing_blockage_placements=blockages,
        closed=closed,
        artifact_id=f"{identity}:fixture-result:{status.value}:{int(closed)}",
    )


def routed_artifacts(
    name: str = "contract-physical-design",
) -> tuple[PhysicalDesignJob, PhysicalDesignResult]:
    job = routed_job(name)
    return job, typed_result(job)


__all__ = ["routed_artifacts", "routed_job", "typed_result"]
