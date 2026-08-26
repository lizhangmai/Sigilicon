"""Technology-, design-, and database-neutral physical-design model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from sigilicon.layout.pnr._serialization import canonical_json


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


class ConstraintMode(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class Axis(str, Enum):
    X = "x"
    Y = "y"


class AlignmentAnchor(str, Enum):
    LOW = "low"
    CENTER = "center"
    HIGH = "high"


class SeparationAxis(str, Enum):
    X = "x"
    Y = "y"
    ANY = "any"


class TechnologyCapability(str, Enum):
    TRACK_ROUTING = "track_routing"
    GRIDLESS_ROUTING = "gridless_routing"
    VIA_DEFINITIONS = "via_definitions"
    VIA_STACKS = "via_stacks"
    MINIMUM_WIDTH_RULES = "minimum_width_rules"
    MINIMUM_SPACING_RULES = "minimum_spacing_rules"
    ENCLOSURE_RULES = "enclosure_rules"
    EXTENSION_RULES = "extension_rules"
    CUT_SPACING_RULES = "cut_spacing_rules"


class PnrStage(str, Enum):
    PLACEMENT = "placement"
    ROUTING = "routing"


class ResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    EXHAUSTED = "exhausted"


class ConstraintStatus(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNSUPPORTED = "unsupported"
    NOT_EVALUATED = "not_evaluated"


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
class RoutingTrackPattern(CanonicalValue):
    name: str
    layer: str
    axis: Axis
    start_dbu: int
    pitch_dbu: int
    count: int


@dataclass(frozen=True)
class GridlessRoutingResource(CanonicalValue):
    name: str
    layer: str
    region: Rect | None = None


RoutingResource: TypeAlias = RoutingTrackPattern | GridlessRoutingResource


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
class ViaStack(CanonicalValue):
    name: str
    vias: tuple[str, ...]


@dataclass(frozen=True)
class MinimumWidthRule(CanonicalValue):
    name: str
    layer: str
    width_dbu: int


@dataclass(frozen=True)
class MinimumSpacingRule(CanonicalValue):
    name: str
    layer: str
    spacing_dbu: int


@dataclass(frozen=True)
class EnclosureRule(CanonicalValue):
    name: str
    outer_layer: str
    inner_layer: str
    enclosure_x_dbu: int
    enclosure_y_dbu: int


@dataclass(frozen=True)
class ExtensionRule(CanonicalValue):
    name: str
    outer_layer: str
    inner_layer: str
    axis: Axis
    extension_dbu: int


@dataclass(frozen=True)
class CutSpacingRule(CanonicalValue):
    name: str
    cut_layer: str
    spacing_x_dbu: int
    spacing_y_dbu: int


PhysicalRule: TypeAlias = (
    MinimumWidthRule
    | MinimumSpacingRule
    | EnclosureRule
    | ExtensionRule
    | CutSpacingRule
)


@dataclass(frozen=True)
class PhysicalTechnology(CanonicalValue):
    name: str
    dbu_per_micron: int
    manufacturing_grid_dbu: int
    layers: tuple[PhysicalLayer, ...] = ()
    routing_resources: tuple[RoutingResource, ...] = ()
    via_definitions: tuple[ViaDefinition, ...] = ()
    via_stacks: tuple[ViaStack, ...] = ()
    rules: tuple[PhysicalRule, ...] = ()


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


@dataclass(frozen=True)
class FenceConstraint(CanonicalValue):
    name: str
    instances: tuple[str, ...]
    region: Rect
    mode: ConstraintMode = ConstraintMode.HARD
    weight: float = 1.0


@dataclass(frozen=True)
class AlignmentConstraint(CanonicalValue):
    name: str
    instances: tuple[str, ...]
    axis: Axis
    anchor: AlignmentAnchor = AlignmentAnchor.LOW
    mode: ConstraintMode = ConstraintMode.HARD
    weight: float = 1.0


@dataclass(frozen=True)
class OrderingConstraint(CanonicalValue):
    name: str
    first: str
    second: str
    axis: Axis
    minimum_gap_dbu: int = 0
    mode: ConstraintMode = ConstraintMode.HARD
    weight: float = 1.0


@dataclass(frozen=True)
class SymmetryConstraint(CanonicalValue):
    name: str
    pairs: tuple[tuple[str, str], ...]
    axis: Axis
    coordinate_dbu: int
    mode: ConstraintMode = ConstraintMode.HARD
    weight: float = 1.0


@dataclass(frozen=True)
class ArrayConstraint(CanonicalValue):
    name: str
    instances: tuple[str, ...]
    columns: int
    x_pitch_dbu: int
    y_pitch_dbu: int
    require_same_orientation: bool = True
    mode: ConstraintMode = ConstraintMode.HARD
    weight: float = 1.0


@dataclass(frozen=True)
class SeparationConstraint(CanonicalValue):
    name: str
    first: str
    second: str
    minimum_gap_dbu: int
    axis: SeparationAxis = SeparationAxis.ANY
    mode: ConstraintMode = ConstraintMode.HARD
    weight: float = 1.0


PlacementConstraint: TypeAlias = (
    FenceConstraint
    | AlignmentConstraint
    | OrderingConstraint
    | SymmetryConstraint
    | ArrayConstraint
    | SeparationConstraint
)


@dataclass(frozen=True)
class BoundingBoxAreaObjective(CanonicalValue):
    name: str
    weight: float = 1.0


@dataclass(frozen=True)
class EstimatedHpwlObjective(CanonicalValue):
    name: str
    weight: float = 1.0


@dataclass(frozen=True)
class DensityOverflowObjective(CanonicalValue):
    name: str
    bins_x: int
    bins_y: int
    target_density: float
    weight: float = 1.0


@dataclass(frozen=True)
class BoundingBoxCongestionObjective(CanonicalValue):
    name: str
    bins_x: int
    bins_y: int
    weight: float = 1.0


PlacementObjective: TypeAlias = (
    BoundingBoxAreaObjective
    | EstimatedHpwlObjective
    | DensityOverflowObjective
    | BoundingBoxCongestionObjective
)


@dataclass(frozen=True)
class RoutingLayerConstraint(CanonicalValue):
    name: str
    net: str
    allowed_layers: tuple[str, ...]


@dataclass(frozen=True)
class RoutingLengthConstraint(CanonicalValue):
    name: str
    net: str
    minimum_length_dbu: int = 0
    maximum_length_dbu: int | None = None


@dataclass(frozen=True)
class RoutingViaCountConstraint(CanonicalValue):
    name: str
    net: str
    maximum_vias: int


RoutingConstraint: TypeAlias = (
    RoutingLayerConstraint
    | RoutingLengthConstraint
    | RoutingViaCountConstraint
)


@dataclass(frozen=True)
class PnrRequest(CanonicalValue):
    stages: tuple[PnrStage, ...] = (PnrStage.PLACEMENT,)
    minimum_instance_spacing_dbu: int = 0
    maximum_search_states: int = 100_000
    objectives: tuple[PlacementObjective, ...] = ()
    required_technology_capabilities: tuple[TechnologyCapability, ...] = ()
    maximum_route_states: int = 200_000
    maximum_routing_iterations: int = 8
    routing_congestion_bins_x: int = 8
    routing_congestion_bins_y: int = 8


@dataclass(frozen=True)
class PhysicalDesignJob(CanonicalValue):
    technology: PhysicalTechnology
    design: PhysicalDesign
    constraints: tuple[PlacementConstraint, ...] = ()
    request: PnrRequest = PnrRequest()
    routing_constraints: tuple[RoutingConstraint, ...] = ()


@dataclass(frozen=True)
class InstancePlacement(CanonicalValue):
    instance: str
    placement: Placement


@dataclass(frozen=True)
class Diagnostic(CanonicalValue):
    code: str
    message: str
    entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConstraintOutcome(CanonicalValue):
    constraint: str
    status: ConstraintStatus
    message: str


@dataclass(frozen=True)
class Metric(CanonicalValue):
    name: str
    value: int | float
    unit: str


@dataclass(frozen=True)
class StageReport(CanonicalValue):
    stage: PnrStage
    status: ResultStatus
    diagnostics: tuple[Diagnostic, ...] = ()
    metrics: tuple[Metric, ...] = ()


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
class PnrProvenance(CanonicalValue):
    engine: str
    engine_version: int
    algorithm: str
    input_sha256: str
    deterministic: bool


@dataclass(frozen=True)
class PhysicalDesignResult(CanonicalValue):
    status: ResultStatus
    placements: tuple[InstancePlacement, ...]
    constraint_outcomes: tuple[ConstraintOutcome, ...]
    stage_reports: tuple[StageReport, ...]
    provenance: PnrProvenance
    routes: tuple[NetRoute, ...] = ()
