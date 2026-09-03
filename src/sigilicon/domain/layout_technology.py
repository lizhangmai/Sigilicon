"""Typed, project-neutral technology roles for custom layout."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from sigilicon.contracts import read_toml, require_config_header


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_REQUIRED_LAYERS = frozenset(
    {
        "routing1",
        "routing2",
        "routing3",
        "diffusion",
        "p_implant",
        "n_implant",
        "n_well",
    }
)
_REQUIRED_VIAS = frozenset(
    {
        "substrate_tap",
        "well_tap",
        "routing1_routing2",
        "routing2_routing3",
    }
)
_HEADER_FIELDS = frozenset({"schema", "contract_kind", "path_scope", "owner"})


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _table(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _reject_unknown(
    raw: Mapping[str, object], allowed: frozenset[str] | set[str], field: str
) -> None:
    if unknown := set(raw) - set(allowed):
        raise ValueError(f"{field} contains unsupported fields: {sorted(unknown)}")


def _pcell_parameters(
    value: object, field_name: str
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a PCell parameter array")
    result: list[tuple[str, str, str]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not all(isinstance(part, str) and part for part in item)
        ):
            raise ValueError(
                f"{field_name}[{index}] must contain name, kind, value"
            )
        name, value_kind, parameter_value = item
        if value_kind not in {"string", "int", "float", "boolean"}:
            raise ValueError(f"{field_name}[{index}] has unsupported value kind")
        result.append(
            (
                _identifier(name, f"{field_name}[{index}] name"),
                value_kind,
                parameter_value,
            )
        )
    if len({item[0] for item in result}) != len(result):
        raise ValueError(f"{field_name} contains duplicate parameters")
    return tuple(result)


@dataclass(frozen=True)
class MosPcellInterface:
    length_parameter: str
    width_parameter: str
    finger_count_parameter: str
    source_terminal: str
    drain_terminal: str
    source_alias_prefix: str
    drain_alias_prefix: str
    cdf_callback_parameter: str
    cdf_callback_bypass_parameters: tuple[str, ...]
    gate_contact_value: str
    gate_contact_enhancement_parameter: str
    gate_contact_enhancement_value: str
    gate_contact_parameters: tuple[tuple[str, str, str], ...] = field(
        default=(), kw_only=True
    )
    pmos_contact_parameters: tuple[tuple[str, str, str], ...] = field(
        default=(), kw_only=True
    )


@dataclass(frozen=True)
class MomPcellInterface:
    cell_name: str
    plus_terminal: str
    minus_terminal: str
    finger_width_parameter: str
    finger_spacing_parameter: str
    finger_length_parameter: str
    finger_count_parameter: str
    start_metal_parameter: str
    stop_metal_parameter: str
    multiplicity_parameter: str


@dataclass(frozen=True)
class ResistorPcellInterface:
    cell_name: str
    plus_terminal: str
    minus_terminal: str
    length_parameter: str
    width_parameter: str
    calculation_parameter: str
    calculation_value: str
    multiplicity_parameter: str


@dataclass(frozen=True)
class LayoutTechnology:
    """Validated technology roles consumed by project-owned layout recipes."""

    owner: str
    model_polarities: Mapping[str, str]
    layers: Mapping[str, str]
    vias: Mapping[str, str]
    via_landings: Mapping[str, Mapping[str, tuple[int, int]]]
    mos_pcell: MosPcellInterface
    mom_pcell: MomPcellInterface | None = field(default=None, kw_only=True)
    resistor_pcell: ResistorPcellInterface | None = field(default=None, kw_only=True)

    def layer(self, role: str) -> str:
        try:
            return self.layers[role]
        except KeyError as exc:
            raise ValueError(f"{self.owner} has no layout layer role {role!r}") from exc

    def via(self, role: str) -> str:
        try:
            return self.vias[role]
        except KeyError as exc:
            raise ValueError(f"{self.owner} has no layout via role {role!r}") from exc

    def polarity(self, model: str) -> str:
        try:
            return self.model_polarities[model]
        except KeyError as exc:
            raise ValueError(f"{self.owner} has no polarity for model {model!r}") from exc

    def via_landing_half_size(
        self,
        via_role: str,
        layer_role: str,
    ) -> tuple[int, int]:
        try:
            return self.via_landings[via_role][layer_role]
        except KeyError as exc:
            raise ValueError(
                f"{self.owner} via role {via_role!r} has no {layer_role!r} landing"
            ) from exc

    def require_mom_pcell(self) -> MomPcellInterface:
        if self.mom_pcell is None:
            raise ValueError(f"{self.owner} does not declare a MOM PCell interface")
        return self.mom_pcell

    def require_resistor_pcell(self) -> ResistorPcellInterface:
        if self.resistor_pcell is None:
            raise ValueError(f"{self.owner} does not declare a resistor PCell interface")
        return self.resistor_pcell

def load_layout_technology(
    path: Path,
    *,
    contract_kind: str,
    owner: str,
    path_scope: str = "owner",
    payload_key: str | None = None,
) -> LayoutTechnology:
    """Load common roles from a native contract or a named domain payload."""

    contract = path.resolve()
    raw = read_toml(contract)
    require_config_header(
        raw,
        contract,
        contract_kind=contract_kind,
        path_scope=path_scope,
        owner=owner,
    )
    return parse_layout_technology(raw, owner=owner, payload_key=payload_key)


def parse_layout_technology(
    raw: Mapping[str, object],
    *,
    owner: str,
    payload_key: str | None = None,
) -> LayoutTechnology:
    """Parse custom-layout roles from an already validated domain document."""

    if payload_key is None:
        technology_raw = raw
    else:
        technology_raw = _table(raw.get(payload_key), payload_key)
    _reject_unknown(
        technology_raw,
        (_HEADER_FIELDS if payload_key is None else frozenset())
        | {
            "model_polarities",
            "layers",
            "vias",
            "via_landings",
            "mom_pcell",
            "resistor_pcell",
            "mos_pcell",
        },
        f"{owner} layout technology",
    )
    model_raw = _table(
        technology_raw.get("model_polarities"), "model_polarities"
    )
    model_polarities = {
        _identifier(name, "model name"): _identifier(value, f"model_polarities.{name}")
        for name, value in model_raw.items()
    }
    if not model_polarities or set(model_polarities.values()) - {"nmos", "pmos"}:
        raise ValueError("model polarities must be nmos or pmos")
    layers_raw = _table(technology_raw.get("layers"), "layers")
    layers = {
        _identifier(role, "layer role"): _identifier(value, f"layers.{role}")
        for role, value in layers_raw.items()
    }
    if missing := _REQUIRED_LAYERS - set(layers):
        raise ValueError(f"layout layer roles are missing: {sorted(missing)}")
    vias_raw = _table(technology_raw.get("vias"), "vias")
    vias = {
        _identifier(role, "via role"): _identifier(value, f"vias.{role}")
        for role, value in vias_raw.items()
    }
    if missing := _REQUIRED_VIAS - set(vias):
        raise ValueError(f"layout via roles are missing: {sorted(missing)}")
    landing_raw = _table(technology_raw.get("via_landings"), "via_landings")
    if set(landing_raw) != set(vias):
        raise ValueError("via_landings must define every and only the declared via roles")
    via_landings: dict[str, dict[str, tuple[int, int]]] = {}
    for via_role, value in landing_raw.items():
        role = _identifier(via_role, "via landing role")
        layer_landings = _table(value, f"via_landings.{role}")
        if not layer_landings:
            raise ValueError(f"via_landings.{role} must not be empty")
        parsed: dict[str, tuple[int, int]] = {}
        for layer_role, half_size in layer_landings.items():
            layer = _identifier(layer_role, f"via_landings.{role} layer")
            if layer not in layers:
                raise ValueError(f"via_landings.{role} uses unknown layer role {layer!r}")
            if (
                not isinstance(half_size, list)
                or len(half_size) != 2
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item <= 0
                    for item in half_size
                )
            ):
                raise ValueError(
                    f"via_landings.{role}.{layer} must be two positive DBU integers"
                )
            parsed[layer] = (half_size[0], half_size[1])
        via_landings[role] = parsed
    mom_pcell: MomPcellInterface | None = None
    if "mom_pcell" in technology_raw:
        mom_raw = _table(technology_raw.get("mom_pcell"), "mom_pcell")
        mom_fields = {
            "cell_name",
            "plus_terminal",
            "minus_terminal",
            "finger_width_parameter",
            "finger_spacing_parameter",
            "finger_length_parameter",
            "finger_count_parameter",
            "start_metal_parameter",
            "stop_metal_parameter",
            "multiplicity_parameter",
        }
        _reject_unknown(mom_raw, mom_fields, "mom_pcell")
        mom_pcell = MomPcellInterface(
            **{
                field: _identifier(mom_raw.get(field), f"mom_pcell.{field}")
                for field in mom_fields
            }
        )
    resistor_pcell: ResistorPcellInterface | None = None
    if "resistor_pcell" in technology_raw:
        resistor_raw = _table(
            technology_raw.get("resistor_pcell"), "resistor_pcell"
        )
        resistor_fields = {
            "cell_name",
            "plus_terminal",
            "minus_terminal",
            "length_parameter",
            "width_parameter",
            "calculation_parameter",
            "calculation_value",
            "multiplicity_parameter",
        }
        _reject_unknown(resistor_raw, resistor_fields, "resistor_pcell")
        calculation_value = resistor_raw.get("calculation_value")
        if not isinstance(calculation_value, str) or not calculation_value:
            raise ValueError("resistor_pcell.calculation_value must be a string")
        resistor_pcell = ResistorPcellInterface(
            cell_name=_identifier(
                resistor_raw.get("cell_name"), "resistor_pcell.cell_name"
            ),
            plus_terminal=_identifier(
                resistor_raw.get("plus_terminal"),
                "resistor_pcell.plus_terminal",
            ),
            minus_terminal=_identifier(
                resistor_raw.get("minus_terminal"),
                "resistor_pcell.minus_terminal",
            ),
            length_parameter=_identifier(
                resistor_raw.get("length_parameter"),
                "resistor_pcell.length_parameter",
            ),
            width_parameter=_identifier(
                resistor_raw.get("width_parameter"),
                "resistor_pcell.width_parameter",
            ),
            calculation_parameter=_identifier(
                resistor_raw.get("calculation_parameter"),
                "resistor_pcell.calculation_parameter",
            ),
            calculation_value=calculation_value,
            multiplicity_parameter=_identifier(
                resistor_raw.get("multiplicity_parameter"),
                "resistor_pcell.multiplicity_parameter",
            ),
        )
    pcell_raw = _table(technology_raw.get("mos_pcell"), "mos_pcell")
    pcell_fields = {
        "length_parameter",
        "width_parameter",
        "finger_count_parameter",
        "source_terminal",
        "drain_terminal",
        "source_alias_prefix",
        "drain_alias_prefix",
        "cdf_callback_parameter",
        "cdf_callback_bypass_parameters",
        "gate_contact_value",
        "gate_contact_enhancement_parameter",
        "gate_contact_enhancement_value",
        "gate_contact_parameters",
        "pmos_contact_parameters",
    }
    _reject_unknown(pcell_raw, pcell_fields, "mos_pcell")
    bypass = pcell_raw.get("cdf_callback_bypass_parameters")
    if not isinstance(bypass, list) or any(not isinstance(item, str) for item in bypass):
        raise ValueError("cdf_callback_bypass_parameters must be an identifier array")
    bypass_parameters = tuple(
        _identifier(item, "cdf_callback_bypass_parameters entry") for item in bypass
    )
    if len(set(bypass_parameters)) != len(bypass_parameters):
        raise ValueError("cdf_callback_bypass_parameters contains duplicates")
    return LayoutTechnology(
        owner=owner,
        model_polarities=MappingProxyType(model_polarities),
        layers=MappingProxyType(layers),
        vias=MappingProxyType(vias),
        via_landings=MappingProxyType(
            {
                role: MappingProxyType(landings)
                for role, landings in via_landings.items()
            }
        ),
        mom_pcell=mom_pcell,
        resistor_pcell=resistor_pcell,
        mos_pcell=MosPcellInterface(
            length_parameter=_identifier(
                pcell_raw.get("length_parameter"),
                "mos_pcell.length_parameter",
            ),
            width_parameter=_identifier(
                pcell_raw.get("width_parameter"),
                "mos_pcell.width_parameter",
            ),
            finger_count_parameter=_identifier(
                pcell_raw.get("finger_count_parameter"),
                "mos_pcell.finger_count_parameter",
            ),
            source_terminal=_identifier(
                pcell_raw.get("source_terminal"), "mos_pcell.source_terminal"
            ),
            drain_terminal=_identifier(
                pcell_raw.get("drain_terminal"), "mos_pcell.drain_terminal"
            ),
            source_alias_prefix=_identifier(
                pcell_raw.get("source_alias_prefix"), "mos_pcell.source_alias_prefix"
            ),
            drain_alias_prefix=_identifier(
                pcell_raw.get("drain_alias_prefix"), "mos_pcell.drain_alias_prefix"
            ),
            cdf_callback_parameter=_identifier(
                pcell_raw.get("cdf_callback_parameter"),
                "mos_pcell.cdf_callback_parameter",
            ),
            cdf_callback_bypass_parameters=bypass_parameters,
            gate_contact_value=_identifier(
                pcell_raw.get("gate_contact_value"),
                "mos_pcell.gate_contact_value",
            ),
            gate_contact_enhancement_parameter=_identifier(
                pcell_raw.get("gate_contact_enhancement_parameter"),
                "mos_pcell.gate_contact_enhancement_parameter",
            ),
            gate_contact_enhancement_value=_identifier(
                pcell_raw.get("gate_contact_enhancement_value"),
                "mos_pcell.gate_contact_enhancement_value",
            ),
            gate_contact_parameters=_pcell_parameters(
                pcell_raw.get("gate_contact_parameters", []),
                "mos_pcell.gate_contact_parameters",
            ),
            pmos_contact_parameters=_pcell_parameters(
                pcell_raw.get("pmos_contact_parameters", []),
                "mos_pcell.pmos_contact_parameters",
            ),
        ),
    )


__all__ = [
    "LayoutTechnology",
    "MomPcellInterface",
    "MosPcellInterface",
    "ResistorPcellInterface",
    "load_layout_technology",
    "parse_layout_technology",
]
