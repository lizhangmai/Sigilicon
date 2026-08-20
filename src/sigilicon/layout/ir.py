"""Stable, audited boundary between Laygo2 objects and OA writers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping


@dataclass(frozen=True)
class LayoutInstance:
    name: str
    library: str
    cell: str
    view: str
    origin_dbu: tuple[int, int]
    transform: str
    parameters: tuple[tuple[str, str, str], ...]
    terminals: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LayoutRect:
    name: str
    layer: str
    purpose: str
    bbox_dbu: tuple[tuple[int, int], tuple[int, int]]
    net: str | None = None


@dataclass(frozen=True)
class LayoutPin:
    name: str
    direction: str
    layer: str
    purpose: str
    bbox_dbu: tuple[tuple[int, int], tuple[int, int]]


@dataclass(frozen=True)
class LayoutVia:
    name: str
    via_definition: str
    origin_dbu: tuple[int, int]
    transform: str
    net: str | None = None


@dataclass(frozen=True)
class LayoutPlan:
    library: str
    cell: str
    view: str
    stage: str
    generator: str
    generator_version: int
    laygo2_version: str
    source_fingerprint: str
    dbu_per_micron: int
    instances: tuple[LayoutInstance, ...]
    rectangles: tuple[LayoutRect, ...] = ()
    pins: tuple[LayoutPin, ...] = ()
    vias: tuple[LayoutVia, ...] = ()

    def payload(self) -> dict[str, Any]:
        """Return the complete plan, including generator provenance."""

        payload = asdict(self)
        # Keep empty via lists out of the compact current representation.
        if not self.vias:
            payload.pop("vias")
        return payload

    def content_payload(self) -> dict[str, Any]:
        """Return only identity and physical content written into OA.

        Generator implementation details and source hashes remain available in
        :meth:`payload` for provenance, but they do not make an unchanged OA
        layout stale.  This projection is the mutation/reuse contract.
        """

        payload = self.payload()
        for field in (
            "generator",
            "generator_version",
            "laygo2_version",
            "source_fingerprint",
        ):
            payload.pop(field)
        return payload

    def canonical_json(self) -> str:
        return json.dumps(
            self.payload(), sort_keys=True, indent=2, ensure_ascii=False
        ) + "\n"

    @property
    def fingerprint(self) -> str:
        """Fingerprint OA identity, geometry, parameters and connectivity."""

        encoded = json.dumps(
            self.content_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _int_pair(value: Any, label: str) -> tuple[int, int]:
    values = getattr(value, "tolist", lambda: value)()
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise ValueError(f"{label} must contain two coordinates")
    if any(isinstance(item, bool) or int(item) != item for item in values):
        raise ValueError(f"{label} coordinates must be integral DBU values")
    return int(values[0]), int(values[1])


def lower_laygo2_design(
    design: Any,
    *,
    library: str,
    cell: str,
    view: str,
    stage: str,
    generator: str,
    generator_version: int,
    laygo2_version: str,
    source_fingerprint: str,
    dbu_per_micron: int,
    terminal_maps: Mapping[str, Mapping[str, str]],
    directions: Mapping[str, str],
    instance_views: Mapping[str, str] | None = None,
) -> LayoutPlan:
    """Lower only the deliberately supported Laygo2 subset.

    Keeping this boundary narrow prevents an upstream exporter change from
    silently changing OA semantics. Unsupported Laygo2 objects fail closed.
    """

    import laygo2

    instances: list[LayoutInstance] = []
    rectangles: list[LayoutRect] = []
    pins: list[LayoutPin] = []
    vias: list[LayoutVia] = []
    resolved_instance_views = instance_views or {}
    for object_name, obj in design.items():
        if isinstance(obj, laygo2.object.physical.Instance):
            if obj.shape is not None:
                raise ValueError("mosaic instances are not supported by the OA pilot")
            if obj.transform not in {"R0", "R90", "R180", "R270", "MX", "MY"}:
                raise ValueError(f"unsupported instance transform: {obj.transform}")
            raw_params = (obj.params or {}).get("pcell_params", ())
            parameters: list[tuple[str, str, str]] = []
            for item in raw_params:
                if not isinstance(item, (list, tuple)) or len(item) != 3:
                    raise ValueError(f"invalid PCell parameter for {object_name}: {item!r}")
                name, value_type, value = item
                if value_type not in {"string", "int", "float", "boolean"}:
                    raise ValueError(f"unsupported PCell value type: {value_type!r}")
                parameters.append((str(name), str(value_type), str(value)))
            terminals = terminal_maps.get(str(object_name))
            if terminals is None:
                raise ValueError(f"missing terminal map for {object_name}")
            instances.append(
                LayoutInstance(
                    name=str(object_name),
                    library=str(obj.libname),
                    cell=str(obj.cellname),
                    view=str(
                        resolved_instance_views.get(str(object_name), obj.viewname)
                    ),
                    origin_dbu=_int_pair(obj.xy, f"instance {object_name} origin"),
                    transform=str(obj.transform),
                    parameters=tuple(parameters),
                    terminals=tuple(
                        (str(name), str(net)) for name, net in terminals.items()
                    ),
                )
            )
        elif isinstance(obj, laygo2.object.physical.Rect):
            layer = tuple(str(item) for item in obj.layer)
            if len(layer) != 2:
                raise ValueError(f"rectangle {object_name} needs layer and purpose")
            xy = getattr(obj.xy, "tolist", lambda: obj.xy)()
            rectangles.append(
                LayoutRect(
                    name=str(object_name),
                    layer=layer[0],
                    purpose=layer[1],
                    bbox_dbu=(
                        _int_pair(xy[0], f"rectangle {object_name} lower-left"),
                        _int_pair(xy[1], f"rectangle {object_name} upper-right"),
                    ),
                    net=None if obj.netname is None else str(obj.netname),
                )
            )
        elif isinstance(obj, laygo2.object.physical.Pin):
            layer = tuple(str(item) for item in obj.layer)
            if len(layer) != 2:
                raise ValueError(f"pin {object_name} needs layer and purpose")
            name = str(obj.netname or obj.name or object_name)
            if name not in directions:
                raise ValueError(f"pin {name} has no canonical direction")
            xy = getattr(obj.xy, "tolist", lambda: obj.xy)()
            pins.append(
                LayoutPin(
                    name=name,
                    direction=directions[name],
                    layer=layer[0],
                    purpose=layer[1],
                    bbox_dbu=(
                        _int_pair(xy[0], f"pin {name} lower-left"),
                        _int_pair(xy[1], f"pin {name} upper-right"),
                    ),
                )
            )
        elif isinstance(obj, laygo2.object.physical.Via):
            via_definition = (obj.params or {}).get("via_definition")
            if not isinstance(via_definition, str) or not via_definition:
                raise ValueError(f"via {object_name} needs a via_definition")
            transform = str(getattr(obj, "transform", "R0"))
            if transform not in {"R0", "R90", "R180", "R270", "MX", "MY"}:
                raise ValueError(f"unsupported via transform: {transform}")
            vias.append(
                LayoutVia(
                    name=str(object_name),
                    via_definition=via_definition,
                    origin_dbu=_int_pair(obj.xy, f"via {object_name} origin"),
                    transform=transform,
                    net=None if obj.netname is None else str(obj.netname),
                )
            )
        else:
            raise ValueError(
                f"unsupported Laygo2 object {object_name}: {type(obj).__name__}"
            )
    if len({item.name for item in instances}) != len(instances):
        raise ValueError("layout plan contains duplicate instance names")
    if stage == "routed" and {item.name for item in pins} != set(directions):
        raise ValueError("routed plan pins must exactly match the canonical interface")
    if stage == "placement_probe" and (rectangles or pins or vias):
        raise ValueError("placement_probe plans may contain only PCell instances")
    return LayoutPlan(
        library=library,
        cell=cell,
        view=view,
        stage=stage,
        generator=generator,
        generator_version=generator_version,
        laygo2_version=laygo2_version,
        source_fingerprint=source_fingerprint,
        dbu_per_micron=dbu_per_micron,
        instances=tuple(instances),
        rectangles=tuple(rectangles),
        pins=tuple(pins),
        vias=tuple(vias),
    )
