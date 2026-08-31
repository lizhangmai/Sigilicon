"""Technology-, design-, and database-neutral physical-design model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from sigilicon.canonical import canonical_json


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


class PhysicalDesignStage(str, Enum):
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


class PhysicalOwnerKind(str, Enum):
    INSTANCE = "instance"
    PIN = "pin"
    PORT = "port"
    BLOCKAGE = "blockage"


@dataclass(frozen=True)
class Point(CanonicalValue):
    x: int
    y: int


@dataclass(frozen=True)
class PhysicalOwnerIdentity(CanonicalValue):
    """Stable source identity independent of geometry allocation."""

    kind: PhysicalOwnerKind
    locator: tuple[str, ...]

    @property
    def stable_name(self) -> str:
        return ":".join((self.kind.value, *self.locator))


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
class RoutingBlockage(CanonicalValue):
    """Placed top-level routing obstruction with an explicit repair scope.

    Shapes use a local coordinate system bounded by ``width_dbu`` and
    ``height_dbu``.  A missing ``repair_region`` makes the blockage fixed;
    otherwise placement repair may move it only within that region.
    """

    name: str
    width_dbu: int
    height_dbu: int
    shapes: tuple[LayerShape, ...]
    placement: Placement
    repair_region: Rect | None = None
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


@dataclass(frozen=True)
class RoutingSkewConstraint(CanonicalValue):
    name: str
    nets: tuple[str, ...]
    maximum_skew_dbu: int


@dataclass(frozen=True)
class RoutingRegionConstraint(CanonicalValue):
    name: str
    net: str
    required_regions: tuple[LayerShape, ...]


@dataclass(frozen=True)
class RoutingShieldConstraint(CanonicalValue):
    name: str
    signal_net: str
    shield_net: str
    maximum_spacing_dbu: int
    layers: tuple[str, ...] = ()


RoutingConstraint: TypeAlias = (
    RoutingLayerConstraint
    | RoutingLengthConstraint
    | RoutingViaCountConstraint
    | RoutingSkewConstraint
    | RoutingRegionConstraint
    | RoutingShieldConstraint
)


@dataclass(frozen=True)
class PhysicalDesignRequest(CanonicalValue):
    stages: tuple[PhysicalDesignStage, ...] = (PhysicalDesignStage.PLACEMENT,)
    minimum_instance_spacing_dbu: int = 0
    objectives: tuple[PlacementObjective, ...] = ()
    required_technology_capabilities: tuple[TechnologyCapability, ...] = ()


@dataclass(frozen=True)
class PhysicalDesignJob(CanonicalValue):
    technology: PhysicalTechnology
    design: PhysicalDesign
    constraints: tuple[PlacementConstraint, ...] = ()
    request: PhysicalDesignRequest = PhysicalDesignRequest()
    routing_constraints: tuple[RoutingConstraint, ...] = ()


@dataclass(frozen=True)
class InstancePlacement(CanonicalValue):
    instance: str
    placement: Placement


@dataclass(frozen=True)
class RoutingBlockagePlacement(CanonicalValue):
    blockage: str
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
    stage: PhysicalDesignStage
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
class PhysicalDesignProvenance(CanonicalValue):
    backend: str
    job_identity: str
    deterministic: bool


@dataclass(frozen=True)
class PhysicalDesignResult(CanonicalValue):
    status: ResultStatus
    placements: tuple[InstancePlacement, ...]
    constraint_outcomes: tuple[ConstraintOutcome, ...]
    stage_reports: tuple[StageReport, ...]
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
