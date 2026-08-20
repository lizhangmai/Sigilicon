"""Validated declarative technology and geometry inputs for layout algorithms."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header

_POLARITIES = {"nmos", "pmos"}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_REQUIRED_LAYERS = {
    "routing1",
    "routing2",
    "routing3",
    "diffusion",
    "p_implant",
    "n_implant",
    "n_well",
}
_REQUIRED_VIAS = {
    "substrate_tap",
    "well_tap",
    "routing1_routing2",
    "routing2_routing3",
}


@dataclass(frozen=True)
class PcellParameter:
    name: str
    value_type: str
    value: str


@dataclass(frozen=True)
class MosPcellInterface:
    length_parameter: str
    width_parameter: str
    gate_contact_selection_parameter: str
    gate_contact_parameters: tuple[PcellParameter, ...]
    polarity_parameters: Mapping[str, tuple[PcellParameter, ...]]


@dataclass(frozen=True)
class ViaInterface:
    definition: str
    landing_half_sizes: Mapping[str, tuple[int, int]]


@dataclass(frozen=True)
class LayoutTechnologyInterface:
    model_polarities: Mapping[str, str]
    layers: Mapping[str, str]
    vias: Mapping[str, ViaInterface]
    mos_pcell: MosPcellInterface

    def layer(self, role: str) -> str:
        try:
            return self.layers[role]
        except KeyError as exc:
            raise ValueError(f"layout technology has no layer role {role!r}") from exc

    def via(self, role: str) -> ViaInterface:
        try:
            return self.vias[role]
        except KeyError as exc:
            raise ValueError(f"layout technology has no via role {role!r}") from exc

    def polarity(self, model: str) -> str:
        try:
            return self.model_polarities[model]
        except KeyError as exc:
            raise ValueError(f"layout technology has no polarity for model {model!r}") from exc


@dataclass(frozen=True)
class LayoutGeometryProfile:
    path: Path
    sha256: str
    sections: Mapping[str, Mapping[str, Any]]

    def integer(self, section: str, field: str) -> int:
        value = self._value(section, field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"layout profile {section}.{field} must be an integer")
        return value

    def pair(self, section: str, field: str) -> tuple[int, int]:
        value = self._value(section, field)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        ):
            raise ValueError(f"layout profile {section}.{field} must be an integer pair")
        return value[0], value[1]

    def _value(self, section: str, field: str) -> Any:
        try:
            return self.sections[section][field]
        except KeyError as exc:
            raise ValueError(f"layout profile is missing {section}.{field}") from exc


@dataclass(frozen=True)
class LayoutGenerationConfig:
    technology: LayoutTechnologyInterface
    geometry: LayoutGeometryProfile


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _parameter_list(value: Any, field: str) -> tuple[PcellParameter, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of parameter tables")
    result: list[PcellParameter] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"name", "type", "value"}:
            raise ValueError(f"{field}[{index}] must contain name, type, and value")
        name = _identifier(item["name"], f"{field}[{index}].name")
        value_type = item["type"]
        parameter_value = item["value"]
        if value_type not in {"string", "boolean", "int", "float"}:
            raise ValueError(f"{field}[{index}].type is unsupported")
        if not isinstance(parameter_value, str) or not parameter_value:
            raise ValueError(f"{field}[{index}].value must be a non-empty string")
        result.append(PcellParameter(name, value_type, parameter_value))
    if len({item.name for item in result}) != len(result):
        raise ValueError(f"{field} contains duplicate parameter names")
    return tuple(result)


def _read_toml(path: Path, field: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload = path.read_bytes()
        value = tomllib.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {field} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} root must be a table")
    return payload, value


def load_layout_generation_config(
    layout_pdk_raw: Mapping[str, Any],
    *,
    pdk_path: Path,
) -> LayoutGenerationConfig:
    """Load the PDK interface and its separately versioned geometry profile."""

    raw = layout_pdk_raw.get("generation")
    if not isinstance(raw, dict):
        raise ValueError("pdk.layout.generation must be a table")

    profile_value = raw.get("profile")
    if not isinstance(profile_value, str) or not profile_value:
        raise ValueError("pdk.layout.generation.profile must be a non-empty path")
    profile_path = Path(profile_value).expanduser()
    if not profile_path.is_absolute():
        profile_path = pdk_path.parent / profile_path
    profile_path = profile_path.resolve()
    payload, profile_raw = _read_toml(profile_path, "layout generation profile")
    require_config_header(
        profile_raw,
        profile_path,
        contract_kind="platform-layout-profile",
        path_scope="platform",
        owner="tsmc28",
    )
    sections: dict[str, Mapping[str, Any]] = {}
    for name, section in profile_raw.items():
        if name in {"schema", "contract_kind", "path_scope", "owner"}:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"layout profile {name} must be a table")
        sections[name] = section
    geometry = LayoutGeometryProfile(
        path=profile_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        sections=sections,
    )
    for section, fields in {
        "placement": ("template_bbox", "default_pitch", "routed_pitch"),
        "access": (
            "wire_half_width",
            "source_offset_x",
            "drain_offset_x",
            "gate_offset",
            "gate_landing_half_size",
        ),
    }.items():
        for field in fields:
            if field in {"template_bbox", "default_pitch", "routed_pitch", "gate_offset", "gate_landing_half_size"}:
                geometry.pair(section, field)
            else:
                geometry.integer(section, field)

    model_raw = raw.get("model_polarities")
    if not isinstance(model_raw, dict) or not model_raw:
        raise ValueError("pdk.layout.generation.model_polarities must be a non-empty table")
    model_polarities = {
        _identifier(model, "pdk.layout.generation.model_polarities key"): polarity
        for model, polarity in model_raw.items()
    }
    if any(polarity not in _POLARITIES for polarity in model_polarities.values()):
        raise ValueError("layout model polarity must be nmos or pmos")

    layer_raw = raw.get("layers")
    if not isinstance(layer_raw, dict):
        raise ValueError("pdk.layout.generation.layers must be a table")
    layers = {
        _identifier(role, "pdk.layout.generation.layers key"): _identifier(
            value, f"pdk.layout.generation.layers.{role}"
        )
        for role, value in layer_raw.items()
    }
    missing_layers = _REQUIRED_LAYERS - set(layers)
    if missing_layers:
        raise ValueError(f"layout layer roles are missing: {sorted(missing_layers)}")

    via_raw = raw.get("vias")
    if not isinstance(via_raw, dict):
        raise ValueError("pdk.layout.generation.vias must be a table")
    vias: dict[str, ViaInterface] = {}
    for raw_role, item in via_raw.items():
        role = _identifier(raw_role, "pdk.layout.generation.vias key")
        if not isinstance(item, dict):
            raise ValueError(f"pdk.layout.generation.vias.{role} must be a table")
        definition = _identifier(
            item.get("definition"), f"pdk.layout.generation.vias.{role}.definition"
        )
        landings_raw = item.get("landing_half_sizes")
        if not isinstance(landings_raw, dict) or not landings_raw:
            raise ValueError(
                f"pdk.layout.generation.vias.{role}.landing_half_sizes must be a table"
            )
        landings: dict[str, tuple[int, int]] = {}
        for layer_role, size in landings_raw.items():
            if layer_role not in layers:
                raise ValueError(f"via {role} references unknown layer role {layer_role}")
            if (
                not isinstance(size, list)
                or len(size) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value <= 0
                    for value in size
                )
            ):
                raise ValueError(f"via {role} landing {layer_role} must be a positive pair")
            landings[layer_role] = (size[0], size[1])
        vias[role] = ViaInterface(definition, landings)
    missing_vias = _REQUIRED_VIAS - set(vias)
    if missing_vias:
        raise ValueError(f"layout via roles are missing: {sorted(missing_vias)}")

    pcell_raw = raw.get("mos_pcell")
    if not isinstance(pcell_raw, dict):
        raise ValueError("pdk.layout.generation.mos_pcell must be a table")
    polarity_raw = pcell_raw.get("polarity_parameters", {})
    if not isinstance(polarity_raw, dict):
        raise ValueError("mos_pcell.polarity_parameters must be a table")
    polarity_parameters = {
        polarity: _parameter_list(
            value, f"pdk.layout.generation.mos_pcell.polarity_parameters.{polarity}"
        )
        for polarity, value in polarity_raw.items()
    }
    if any(polarity not in _POLARITIES for polarity in polarity_parameters):
        raise ValueError("mos_pcell polarity parameter key must be nmos or pmos")
    mos_pcell = MosPcellInterface(
        length_parameter=_identifier(
            pcell_raw.get("length_parameter"), "mos_pcell.length_parameter"
        ),
        width_parameter=_identifier(
            pcell_raw.get("width_parameter"), "mos_pcell.width_parameter"
        ),
        gate_contact_selection_parameter=_identifier(
            pcell_raw.get("gate_contact_selection_parameter"),
            "mos_pcell.gate_contact_selection_parameter",
        ),
        gate_contact_parameters=_parameter_list(
            pcell_raw.get("gate_contact_parameters"),
            "pdk.layout.generation.mos_pcell.gate_contact_parameters",
        ),
        polarity_parameters=polarity_parameters,
    )
    return LayoutGenerationConfig(
        technology=LayoutTechnologyInterface(
            model_polarities=model_polarities,
            layers=layers,
            vias=vias,
            mos_pcell=mos_pcell,
        ),
        geometry=geometry,
    )
