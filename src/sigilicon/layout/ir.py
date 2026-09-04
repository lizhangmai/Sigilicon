"""Stable, audited boundary between Laygo2 objects and OA writers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping

from sigilicon.layout._json import (
    array as _array,
    int_pair as _int_pair_payload,
    optional_text as _optional_text,
    positive_int as _positive_int,
    record as _record,
    string_tuple as _string_tuple,
    text as _text,
)


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
    expected_master_terminals: tuple[str, ...] = ()
    callback_parameters: tuple[str, ...] = ()


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
    dbu_per_micron: int
    instances: tuple[LayoutInstance, ...]
    rectangles: tuple[LayoutRect, ...] = ()
    pins: tuple[LayoutPin, ...] = ()
    vias: tuple[LayoutVia, ...] = ()

    def payload(self) -> dict[str, Any]:
        """Return the complete layout plan."""

        payload = asdict(self)
        # Keep empty via lists out of the compact current representation.
        if not self.vias:
            payload.pop("vias")
        return payload

    def canonical_json(self) -> str:
        return json.dumps(
            self.payload(), sort_keys=True, indent=2, ensure_ascii=False
        ) + "\n"

    @classmethod
    def from_payload(cls, value: object) -> "LayoutPlan":
        """Decode the strict process-boundary representation of a plan."""

        raw = _record(
            value,
            "layout plan",
            {
                "library",
                "cell",
                "view",
                "stage",
                "generator",
                "dbu_per_micron",
                "instances",
                "rectangles",
                "pins",
                "vias",
            },
            optional={"rectangles", "pins", "vias"},
        )
        return cls(
            library=_text(raw["library"], "layout plan library"),
            cell=_text(raw["cell"], "layout plan cell"),
            view=_text(raw["view"], "layout plan view"),
            stage=_text(raw["stage"], "layout plan stage"),
            generator=_text(raw["generator"], "layout plan generator"),
            dbu_per_micron=_positive_int(
                raw["dbu_per_micron"], "layout plan dbu_per_micron"
            ),
            instances=tuple(
                _layout_instance(item, index)
                for index, item in enumerate(_array(raw["instances"], "instances"))
            ),
            rectangles=tuple(
                _layout_rect(item, index)
                for index, item in enumerate(
                    _array(raw.get("rectangles", []), "rectangles")
                )
            ),
            pins=tuple(
                _layout_pin(item, index)
                for index, item in enumerate(_array(raw.get("pins", []), "pins"))
            ),
            vias=tuple(
                _layout_via(item, index)
                for index, item in enumerate(_array(raw.get("vias", []), "vias"))
            ),
        )


def _layout_instance(value: object, index: int) -> LayoutInstance:
    label = f"instances[{index}]"
    raw = _record(
        value,
        label,
        {
            "name",
            "library",
            "cell",
            "view",
            "origin_dbu",
            "transform",
            "parameters",
            "terminals",
            "expected_master_terminals",
            "callback_parameters",
        },
        optional={"expected_master_terminals", "callback_parameters"},
    )
    return LayoutInstance(
        name=_text(raw["name"], f"{label}.name"),
        library=_text(raw["library"], f"{label}.library"),
        cell=_text(raw["cell"], f"{label}.cell"),
        view=_text(raw["view"], f"{label}.view"),
        origin_dbu=_int_pair_payload(raw["origin_dbu"], f"{label}.origin_dbu"),
        transform=_text(raw["transform"], f"{label}.transform"),
        parameters=tuple(
            _string_tuple(item, f"{label}.parameters", 3)
            for item in _array(raw["parameters"], f"{label}.parameters")
        ),
        terminals=tuple(
            _string_tuple(item, f"{label}.terminals", 2)
            for item in _array(raw["terminals"], f"{label}.terminals")
        ),
        expected_master_terminals=tuple(
            _text(item, f"{label}.expected_master_terminals")
            for item in _array(
                raw.get("expected_master_terminals", []),
                f"{label}.expected_master_terminals",
            )
        ),
        callback_parameters=tuple(
            _text(item, f"{label}.callback_parameters")
            for item in _array(
                raw.get("callback_parameters", []),
                f"{label}.callback_parameters",
            )
        ),
    )


def _bbox(value: object, label: str) -> tuple[tuple[int, int], tuple[int, int]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must contain two coordinate pairs")
    return (
        _int_pair_payload(value[0], f"{label}[0]"),
        _int_pair_payload(value[1], f"{label}[1]"),
    )


def _layout_rect(value: object, index: int) -> LayoutRect:
    label = f"rectangles[{index}]"
    raw = _record(value, label, {"name", "layer", "purpose", "bbox_dbu", "net"})
    return LayoutRect(
        name=_text(raw["name"], f"{label}.name"),
        layer=_text(raw["layer"], f"{label}.layer"),
        purpose=_text(raw["purpose"], f"{label}.purpose"),
        bbox_dbu=_bbox(raw["bbox_dbu"], f"{label}.bbox_dbu"),
        net=_optional_text(raw["net"], f"{label}.net"),
    )


def _layout_pin(value: object, index: int) -> LayoutPin:
    label = f"pins[{index}]"
    raw = _record(
        value,
        label,
        {"name", "direction", "layer", "purpose", "bbox_dbu"},
    )
    return LayoutPin(
        name=_text(raw["name"], f"{label}.name"),
        direction=_text(raw["direction"], f"{label}.direction"),
        layer=_text(raw["layer"], f"{label}.layer"),
        purpose=_text(raw["purpose"], f"{label}.purpose"),
        bbox_dbu=_bbox(raw["bbox_dbu"], f"{label}.bbox_dbu"),
    )


def _layout_via(value: object, index: int) -> LayoutVia:
    label = f"vias[{index}]"
    raw = _record(
        value,
        label,
        {"name", "via_definition", "origin_dbu", "transform", "net"},
    )
    return LayoutVia(
        name=_text(raw["name"], f"{label}.name"),
        via_definition=_text(raw["via_definition"], f"{label}.via_definition"),
        origin_dbu=_int_pair_payload(raw["origin_dbu"], f"{label}.origin_dbu"),
        transform=_text(raw["transform"], f"{label}.transform"),
        net=_optional_text(raw["net"], f"{label}.net"),
    )

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
                    expected_master_terminals=tuple(sorted(str(name) for name in terminals)),
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
        dbu_per_micron=dbu_per_micron,
        instances=tuple(instances),
        rectangles=tuple(rectangles),
        pins=tuple(pins),
        vias=tuple(vias),
    )
