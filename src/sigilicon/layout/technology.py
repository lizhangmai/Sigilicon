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
class LayoutTechnology:
    """Owner-validated technology roles consumed by custom layout recipes."""

    owner: str
    model_polarities: Mapping[str, str]
    layers: Mapping[str, str]
    vias: Mapping[str, str]
    via_landings: Mapping[str, Mapping[str, tuple[int, int]]]
    mos_pcell: MosPcellInterface

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
        | {"model_polarities", "layers", "vias", "via_landings", "mos_pcell"},
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
    )


__all__ = ["LayoutTechnology", "MosPcellInterface", "load_layout_technology"]
