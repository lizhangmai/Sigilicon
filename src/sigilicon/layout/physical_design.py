"""Minimal database-neutral geometry contract for physical materialization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256

from sigilicon.canonical import (
    canonical_from_json,
    canonical_json,
)


class CanonicalValue:
    """Value whose complete public state has a deterministic representation."""

    def canonical_json(self) -> str:
        return canonical_json(self)


class Orientation(str, Enum):
    R0 = "R0"
    R90 = "R90"
    R180 = "R180"
    R270 = "R270"
    MX = "MX"
    MY = "MY"
    MXR90 = "MXR90"
    MYR90 = "MYR90"


class LayerKind(str, Enum):
    ROUTING = "routing"
    CUT = "cut"
    OTHER = "other"


class RoutingDirection(str, Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    ANY = "any"


class ResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class Point(CanonicalValue):
    x: int
    y: int


@dataclass(frozen=True)
class Rect(CanonicalValue):
    x_min: int
    y_min: int
    x_max: int
    y_max: int

    def __post_init__(self) -> None:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("rectangle must have positive width and height")

    @property
    def width(self) -> int:
        return self.x_max - self.x_min

    @property
    def height(self) -> int:
        return self.y_max - self.y_min

    @property
    def area(self) -> int:
        return self.width * self.height

    def contains(self, other: Rect) -> bool:
        return (
            self.x_min <= other.x_min
            and self.y_min <= other.y_min
            and other.x_max <= self.x_max
            and other.y_max <= self.y_max
        )

    def intersection(self, other: Rect) -> Rect | None:
        x_min = max(self.x_min, other.x_min)
        y_min = max(self.y_min, other.y_min)
        x_max = min(self.x_max, other.x_max)
        y_max = min(self.y_max, other.y_max)
        if x_min >= x_max or y_min >= y_max:
            return None
        return Rect(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)


@dataclass(frozen=True)
class PhysicalLayer(CanonicalValue):
    name: str
    kind: LayerKind
    direction: RoutingDirection | None = None


@dataclass(frozen=True)
class LayerShape(CanonicalValue):
    layer: str
    shape: Rect


@dataclass(frozen=True)
class ViaDefinition(CanonicalValue):
    name: str
    lower_layer: str
    cut_layer: str
    upper_layer: str
    lower_shapes: tuple[Rect, ...]
    cut_shapes: tuple[Rect, ...]
    upper_shapes: tuple[Rect, ...]


@dataclass(frozen=True)
class PhysicalTechnology(CanonicalValue):
    name: str
    dbu_per_micron: int
    manufacturing_grid_dbu: int
    layers: tuple[PhysicalLayer, ...] = ()
    via_definitions: tuple[ViaDefinition, ...] = ()


@dataclass(frozen=True)
class PinAccess(CanonicalValue):
    layer: str
    shape: Rect


@dataclass(frozen=True)
class MasterPin(CanonicalValue):
    name: str
    accesses: tuple[PinAccess, ...] = ()


@dataclass(frozen=True)
class PhysicalMaster(CanonicalValue):
    name: str
    width_dbu: int
    height_dbu: int
    pins: tuple[MasterPin, ...] = ()
    obstructions: tuple[LayerShape, ...] = ()
    allowed_orientations: tuple[Orientation, ...] = tuple(Orientation)


@dataclass(frozen=True)
class Placement(CanonicalValue):
    origin: Point
    orientation: Orientation = Orientation.R0


@dataclass(frozen=True)
class PhysicalInstance(CanonicalValue):
    name: str
    master: str
    fixed_placement: Placement | None = None


@dataclass(frozen=True)
class RoutingBlockage(CanonicalValue):
    """Placed top-level routing obstruction included in materialized geometry."""

    name: str
    width_dbu: int
    height_dbu: int
    shapes: tuple[LayerShape, ...]
    placement: Placement
    allowed_orientations: tuple[Orientation, ...] = (Orientation.R0,)


@dataclass(frozen=True)
class PhysicalPort(CanonicalValue):
    name: str
    accesses: tuple[PinAccess, ...] = ()


@dataclass(frozen=True)
class PinReference(CanonicalValue):
    pin: str
    instance: str | None = None


@dataclass(frozen=True)
class PhysicalNet(CanonicalValue):
    name: str
    pins: tuple[PinReference, ...]


@dataclass(frozen=True)
class PhysicalDesign(CanonicalValue):
    name: str
    die: Rect
    masters: tuple[PhysicalMaster, ...]
    instances: tuple[PhysicalInstance, ...]
    ports: tuple[PhysicalPort, ...] = ()
    nets: tuple[PhysicalNet, ...] = ()
    routing_blockages: tuple[RoutingBlockage, ...] = ()


@dataclass(frozen=True)
class PhysicalDesignJob(CanonicalValue):
    technology: PhysicalTechnology
    design: PhysicalDesign


@dataclass(frozen=True)
class InstancePlacement(CanonicalValue):
    instance: str
    placement: Placement


@dataclass(frozen=True)
class RoutingBlockagePlacement(CanonicalValue):
    blockage: str
    placement: Placement


@dataclass(frozen=True)
class RouteSegment(CanonicalValue):
    net: str
    layer: str
    start: Point
    end: Point
    width_dbu: int


@dataclass(frozen=True)
class RouteVia(CanonicalValue):
    net: str
    via_definition: str
    origin: Point


@dataclass(frozen=True)
class NetRoute(CanonicalValue):
    net: str
    segments: tuple[RouteSegment, ...]
    vias: tuple[RouteVia, ...] = ()


@dataclass(frozen=True)
class PhysicalDesignProvenance(CanonicalValue):
    backend: str
    job_identity: str
    deterministic: bool


@dataclass(frozen=True)
class PhysicalDesignResult(CanonicalValue):
    status: ResultStatus
    placements: tuple[InstancePlacement, ...]
    provenance: PhysicalDesignProvenance
    routes: tuple[NetRoute, ...] = ()
    routing_blockage_placements: tuple[RoutingBlockagePlacement, ...] = ()
    closed: bool = False
    artifact_id: str = ""

    def __post_init__(self) -> None:
        if type(self.closed) is not bool:
            raise ValueError("physical-design result closed flag must be boolean")
        if self.closed and self.status is not ResultStatus.SUCCEEDED:
            raise ValueError("only a succeeded physical-design result can be closed")
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise ValueError("physical-design result needs an explicit artifact ID")


def physical_design_job_from_json(text: str) -> PhysicalDesignJob:
    return canonical_from_json(text, PhysicalDesignJob)


def physical_design_result_from_json(text: str) -> PhysicalDesignResult:
    return canonical_from_json(text, PhysicalDesignResult)


def physical_design_job_id(job: PhysicalDesignJob) -> str:
    digest = sha256(canonical_json(job).encode("utf-8")).hexdigest()
    return (
        f"physical-design-job:{job.technology.name}:{job.design.name}:"
        f"sha256:{digest}"
    )


def physical_design_result_id(result: PhysicalDesignResult) -> str:
    return result.artifact_id
