"""Typed technology contract for custom MOS placement and routing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Mapping

from sigilicon.domain.config_contracts import require_config_header


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


@dataclass(frozen=True)
class MosPcellInterface:
    finger_count_parameter: str
    source_terminal: str
    drain_terminal: str
    source_alias_prefix: str
    drain_alias_prefix: str
    cdf_callback_parameter: str
    cdf_callback_bypass_parameters: tuple[str, ...]


@dataclass(frozen=True)
class ContactedMosRecipe:
    pitch_dbu: tuple[int, int]
    wire_half_width_dbu: int
    diffusion_contact_extension_dbu: int
    bottom_gate_contact_y_offset_dbu: int
    supported_gate_length_dbu: int
    gate_contact_parameters: tuple[tuple[str, str, str], ...]
    pmos_contact_parameters: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class LayoutTechnology:
    """Owner-validated technology roles consumed by custom layout recipes."""

    owner: str
    model_polarities: Mapping[str, str]
    layers: Mapping[str, str]
    vias: Mapping[str, str]
    via_landings: Mapping[str, Mapping[str, tuple[int, int]]]
    mos_pcell: MosPcellInterface
    contacted_mos: ContactedMosRecipe

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

    def via_landing_half_size(self, via_role: str, layer_role: str) -> tuple[int, int]:
        try:
            return self.via_landings[via_role][layer_role]
        except KeyError as exc:
            raise ValueError(
                f"{self.owner} via role {via_role!r} has no {layer_role!r} landing"
            ) from exc


def load_layout_technology(
    path: Path,
    *,
    contract_kind: str,
    owner: str,
) -> LayoutTechnology:
    """Load one owner-native contract into the shared technology Interface."""

    contract = path.resolve()
    payload = contract.read_bytes()
    raw = tomllib.loads(payload.decode("utf-8"))
    require_config_header(
        raw,
        contract,
        contract_kind=contract_kind,
        path_scope="owner",
        owner=owner,
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {
            "model_polarities",
            "layers",
            "vias",
            "via_landings",
            "mos_pcell",
            "contacted_mos",
        },
        f"{owner} layout technology",
    )
    model_raw = _table(raw.get("model_polarities"), "model_polarities")
    model_polarities = {
        _identifier(name, "model name"): _identifier(value, f"model_polarities.{name}")
        for name, value in model_raw.items()
    }
    if not model_polarities or set(model_polarities.values()) - {"nmos", "pmos"}:
        raise ValueError("model polarities must be nmos or pmos")
    layers_raw = _table(raw.get("layers"), "layers")
    layers = {
        _identifier(role, "layer role"): _identifier(value, f"layers.{role}")
        for role, value in layers_raw.items()
    }
    if missing := _REQUIRED_LAYERS - set(layers):
        raise ValueError(f"layout layer roles are missing: {sorted(missing)}")
    vias_raw = _table(raw.get("vias"), "vias")
    vias = {
        _identifier(role, "via role"): _identifier(value, f"vias.{role}")
        for role, value in vias_raw.items()
    }
    if missing := _REQUIRED_VIAS - set(vias):
        raise ValueError(f"layout via roles are missing: {sorted(missing)}")
    landing_raw = _table(raw.get("via_landings"), "via_landings")
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
    pcell_raw = _table(raw.get("mos_pcell"), "mos_pcell")
    pcell_fields = {
        "finger_count_parameter",
        "source_terminal",
        "drain_terminal",
        "source_alias_prefix",
        "drain_alias_prefix",
        "cdf_callback_parameter",
        "cdf_callback_bypass_parameters",
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
    contacted_raw = _table(raw.get("contacted_mos"), "contacted_mos")
    contacted_fields = {
        "pitch_dbu",
        "wire_half_width_dbu",
        "diffusion_contact_extension_dbu",
        "bottom_gate_contact_y_offset_dbu",
        "supported_gate_length_dbu",
        "gate_contact_parameters",
        "pmos_contact_parameters",
    }
    _reject_unknown(contacted_raw, contacted_fields, "contacted_mos")

    def positive_integer(field: str) -> int:
        value = contacted_raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"contacted_mos.{field} must be a positive integer")
        return value

    pitch = contacted_raw.get("pitch_dbu")
    if (
        not isinstance(pitch, list)
        or len(pitch) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in pitch
        )
    ):
        raise ValueError("contacted_mos.pitch_dbu must be two positive integers")
    gate_y_offset = contacted_raw.get("bottom_gate_contact_y_offset_dbu")
    if isinstance(gate_y_offset, bool) or not isinstance(gate_y_offset, int):
        raise ValueError(
            "contacted_mos.bottom_gate_contact_y_offset_dbu must be an integer"
        )

    def pcell_parameters(field: str) -> tuple[tuple[str, str, str], ...]:
        value = contacted_raw.get(field)
        if not isinstance(value, list):
            raise ValueError(f"contacted_mos.{field} must be an array")
        result: list[tuple[str, str, str]] = []
        for index, item in enumerate(value):
            if (
                not isinstance(item, list)
                or len(item) != 3
                or not all(isinstance(part, str) and part for part in item)
            ):
                raise ValueError(
                    f"contacted_mos.{field}[{index}] must contain name, kind, value"
                )
            name = _identifier(item[0], f"contacted_mos.{field}[{index}] name")
            if item[1] not in {"string", "int", "float", "boolean"}:
                raise ValueError(
                    f"contacted_mos.{field}[{index}] has unsupported value kind"
                )
            result.append((name, item[1], item[2]))
        if len({item[0] for item in result}) != len(result):
            raise ValueError(f"contacted_mos.{field} contains duplicate parameters")
        return tuple(result)

    return LayoutTechnology(
        owner=owner,
        model_polarities=model_polarities,
        layers=layers,
        vias=vias,
        via_landings=via_landings,
        mos_pcell=MosPcellInterface(
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
        ),
        contacted_mos=ContactedMosRecipe(
            pitch_dbu=(pitch[0], pitch[1]),
            wire_half_width_dbu=positive_integer("wire_half_width_dbu"),
            diffusion_contact_extension_dbu=positive_integer(
                "diffusion_contact_extension_dbu"
            ),
            bottom_gate_contact_y_offset_dbu=gate_y_offset,
            supported_gate_length_dbu=positive_integer("supported_gate_length_dbu"),
            gate_contact_parameters=pcell_parameters("gate_contact_parameters"),
            pmos_contact_parameters=pcell_parameters("pmos_contact_parameters"),
        ),
    )


__all__ = [
    "ContactedMosRecipe",
    "LayoutTechnology",
    "MosPcellInterface",
    "load_layout_technology",
]
