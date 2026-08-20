"""Resolved project platform contracts behind one deep loading interface."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.domain.config_contracts import read_toml, require_config_header
from sigilicon.paths import ProjectContext


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_PLATFORM_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_POLARITIES = {"nmos", "pmos"}
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
_HEADER_FIELDS = {"schema", "contract_kind", "path_scope", "owner"}


@dataclass(frozen=True)
class SimulationModelSet:
    """One caller-selectable simulator model include set."""

    name: str
    file: Path
    sections: tuple[str, ...]
    support_files: tuple[Path, ...] = ()

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.file, *self.support_files)

    @property
    def single_section(self) -> str:
        if len(self.sections) != 1:
            raise ValueError(
                f"platform model set {self.name!r} does not have one default section"
            )
        return self.sections[0]


@dataclass(frozen=True)
class SimulationPlatformConfig:
    """Resolved simulation capability supplied by one project platform."""

    path: Path
    default_model_set: str
    model_sets: Mapping[str, SimulationModelSet]

    @property
    def default(self) -> SimulationModelSet:
        return self.model_set(self.default_model_set)

    def model_set(self, name: str) -> SimulationModelSet:
        try:
            return self.model_sets[name]
        except KeyError as exc:
            raise ValueError(f"platform has no simulation model set {name!r}") from exc


@dataclass(frozen=True)
class PcellPolicy:
    finger_count_parameter: str | None = None
    source_terminal: str = "S"
    drain_terminal: str = "D"
    source_alias_prefix: str = "S_"
    drain_alias_prefix: str = "D_"
    cdf_callback_parameter: str | None = None
    cdf_callback_bypass_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class OaPlatformConfig:
    """OA technology and primitive facts owned by the project platform."""

    path: Path
    technology_library: str
    reference_libraries: tuple[str, ...]
    primitive_masters: tuple[str, ...]
    primitive_subcircuits: Mapping[str, tuple[str, ...]]
    pcell_policy: PcellPolicy


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
            raise ValueError(
                f"layout technology has no polarity for model {model!r}"
            ) from exc


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
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in value
            )
        ):
            raise ValueError(
                f"layout profile {section}.{field} must be an integer pair"
            )
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


@dataclass(frozen=True)
class LayoutPdkConfig:
    """Resolved layout and physical-verification platform capability."""

    configuration_sha256: str
    layout_path: Path
    verification_path: Path
    dbu_per_micron: int
    layermap: Path
    drc_deck: Path
    lvs_deck: Path
    qrc_tech_file: Path
    drc_disabled_defines: Mapping[str, int]
    drc_configuration_warnings: tuple[str, ...]
    drc_waiver_layers: tuple[str, ...]
    generation: LayoutGenerationConfig
    xstream_flatten_pcells: bool = True
    xstream_suppressed_warnings: tuple[str, ...] = ()
    xstream_bin: Path | None = None
    calibre_bin: Path | None = None


@dataclass(frozen=True)
class PdkConfig:
    """Fully resolved platform consumed by reusable flow code."""

    key: str
    path: Path
    owner: str
    name: str
    simulation: SimulationPlatformConfig
    oa: OaPlatformConfig
    layout: LayoutPdkConfig | None
    source_paths: tuple[Path, ...]
    source_sha256: str
    asset_root: Path | None = None
    installation_root_environment: str | None = None


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    return value


def _table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _reject_unknown(
    raw: Mapping[str, Any], allowed: set[str], field: str
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{field} contains unsupported fields: {sorted(unknown)}")


def _names(value: object, field: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not empty):
        raise ValueError(f"{field} must be a string array")
    result = tuple(_identifier(item, f"{field}[]") for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _strings(value: object, field: str, *, empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not empty) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _safe_relative(base: Path, value: object, field: str, *, root: Path) -> Path:
    text = _text(value, field)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must be a safe relative path")
    result = (base / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f"{field} escapes the project")
    return result


def _asset_path(base: Path, value: object, field: str) -> Path:
    text = _text(value, field)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must be a safe asset-relative path")
    return (base / relative).resolve()


def _required_file(base: Path, value: object, field: str) -> Path:
    result = _asset_path(base, value, field)
    if not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    return result


def _optional_executable(base: Path, value: object, field: str) -> Path | None:
    if value is None:
        return None
    result = _asset_path(base, value, field)
    if not result.is_file() or not os.access(result, os.X_OK):
        raise ValueError(f"{field} must be executable: {result}")
    return result


def _contract(
    manifest: Path,
    contracts: Mapping[str, Any],
    name: str,
    *,
    root: Path,
    owner: str,
    required: bool = True,
) -> tuple[Path, Mapping[str, Any]] | None:
    value = contracts.get(name)
    if value is None and not required:
        return None
    path = _safe_relative(
        manifest.parent,
        value,
        f"platform.contracts.{name}",
        root=root,
    )
    raw = read_toml(path)
    require_config_header(
        raw,
        path,
        contract_kind=f"platform-{name}",
        path_scope="platform",
        owner=owner,
    )
    return path, raw


def _load_simulation(
    path: Path, raw: Mapping[str, Any], *, asset_root: Path
) -> SimulationPlatformConfig:
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"default_model_set", "model_sets"},
        "platform simulation contract",
    )
    default_name = _text(raw.get("default_model_set"), "default_model_set")
    sets_raw = _table(raw.get("model_sets"), "model_sets")
    model_sets: dict[str, SimulationModelSet] = {}
    for raw_name, value in sets_raw.items():
        name = _identifier(raw_name, "model_sets key")
        item = _table(value, f"model_sets.{name}")
        _reject_unknown(
            item,
            {"file", "sections", "support_files"},
            f"model_sets.{name}",
        )
        sections = _strings(
            item.get("sections"), f"model_sets.{name}.sections", empty=False
        )
        support_values = item.get("support_files", [])
        support_files = tuple(
            _asset_path(
                asset_root,
                support,
                f"model_sets.{name}.support_files[{index}]",
            )
            for index, support in enumerate(
                _strings(support_values, f"model_sets.{name}.support_files")
            )
        )
        model_sets[name] = SimulationModelSet(
            name=name,
            file=_asset_path(asset_root, item.get("file"), f"model_sets.{name}.file"),
            sections=sections,
            support_files=support_files,
        )
    if default_name not in model_sets:
        raise ValueError("default_model_set must select a declared model set")
    model_sets[default_name].single_section
    return SimulationPlatformConfig(path, default_name, model_sets)


def _optional_policy_identifier(
    raw: Mapping[str, Any], name: str
) -> str | None:
    value = raw.get(name)
    return None if value is None else _identifier(value, f"pcell_policy.{name}")


def _load_pcell_policy(raw: Mapping[str, Any]) -> PcellPolicy:
    _reject_unknown(
        raw,
        {
            "finger_count_parameter",
            "source_terminal",
            "drain_terminal",
            "source_alias_prefix",
            "drain_alias_prefix",
            "cdf_callback_parameter",
            "cdf_callback_bypass_parameters",
        },
        "pcell_policy",
    )
    return PcellPolicy(
        finger_count_parameter=_optional_policy_identifier(
            raw, "finger_count_parameter"
        ),
        source_terminal=_identifier(raw.get("source_terminal", "S"), "source_terminal"),
        drain_terminal=_identifier(raw.get("drain_terminal", "D"), "drain_terminal"),
        source_alias_prefix=_identifier(
            raw.get("source_alias_prefix", "S_"), "source_alias_prefix"
        ),
        drain_alias_prefix=_identifier(
            raw.get("drain_alias_prefix", "D_"), "drain_alias_prefix"
        ),
        cdf_callback_parameter=_optional_policy_identifier(
            raw, "cdf_callback_parameter"
        ),
        cdf_callback_bypass_parameters=_names(
            raw.get("cdf_callback_bypass_parameters", []),
            "cdf_callback_bypass_parameters",
            empty=True,
        ),
    )


def _load_oa(path: Path, raw: Mapping[str, Any]) -> OaPlatformConfig:
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {
            "technology_library",
            "reference_libraries",
            "primitive_masters",
            "primitive_subcircuits",
            "pcell_policy",
        },
        "platform OA contract",
    )
    primitive_masters = _names(
        raw.get("primitive_masters", []), "primitive_masters", empty=True
    )
    primitive_raw = _table(raw.get("primitive_subcircuits", {}), "primitive_subcircuits")
    primitive_subcircuits = {
        _identifier(master, "primitive_subcircuits key"): _names(
            terminals, f"primitive_subcircuits.{master}"
        )
        for master, terminals in primitive_raw.items()
    }
    unknown = set(primitive_subcircuits) - set(primitive_masters)
    if unknown:
        raise ValueError(
            f"primitive_subcircuits contains undeclared masters: {sorted(unknown)}"
        )
    return OaPlatformConfig(
        path=path,
        technology_library=_identifier(
            raw.get("technology_library"), "technology_library"
        ),
        reference_libraries=_names(raw.get("reference_libraries"), "reference_libraries"),
        primitive_masters=primitive_masters,
        primitive_subcircuits=primitive_subcircuits,
        pcell_policy=_load_pcell_policy(
            _table(raw.get("pcell_policy", {}), "pcell_policy")
        ),
    )


def _parameter_list(value: object, field: str) -> tuple[PcellParameter, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of parameter tables")
    result: list[PcellParameter] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"name", "type", "value"}:
            raise ValueError(f"{field}[{index}] must contain name, type, and value")
        name = _identifier(item["name"], f"{field}[{index}].name")
        value_type = item["type"]
        parameter_value = item["value"]
        if value_type not in {"string", "boolean", "int", "float"}:
            raise ValueError(f"{field}[{index}].type is unsupported")
        if not isinstance(parameter_value, str) or not parameter_value:
            raise ValueError(f"{field}[{index}].value must be non-empty text")
        result.append(PcellParameter(name, value_type, parameter_value))
    if len({item.name for item in result}) != len(result):
        raise ValueError(f"{field} contains duplicate parameter names")
    return tuple(result)


def _load_geometry(path: Path, *, owner: str) -> LayoutGeometryProfile:
    payload = path.read_bytes()
    raw = read_toml(path)
    require_config_header(
        raw,
        path,
        contract_kind="platform-layout-profile",
        path_scope="platform",
        owner=owner,
    )
    sections: dict[str, Mapping[str, Any]] = {}
    for name, section in raw.items():
        if name in {"schema", "contract_kind", "path_scope", "owner"}:
            continue
        sections[name] = _table(section, f"layout profile {name}")
    geometry = LayoutGeometryProfile(
        path=path,
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
            if field in {
                "template_bbox",
                "default_pitch",
                "routed_pitch",
                "gate_offset",
                "gate_landing_half_size",
            }:
                geometry.pair(section, field)
            else:
                geometry.integer(section, field)
    return geometry


def _load_generation(
    path: Path,
    raw: Mapping[str, Any],
    *,
    root: Path,
    owner: str,
) -> LayoutGenerationConfig:
    generation = _table(raw.get("generation"), "layout.generation")
    _reject_unknown(
        generation,
        {"profile", "model_polarities", "layers", "vias", "mos_pcell"},
        "layout.generation",
    )
    profile_path = _safe_relative(
        path.parent,
        generation.get("profile"),
        "layout.generation.profile",
        root=root,
    )
    geometry = _load_geometry(profile_path, owner=owner)
    model_raw = _table(generation.get("model_polarities"), "model_polarities")
    model_polarities = {
        _identifier(model, "model_polarities key"): _text(
            polarity, f"model_polarities.{model}"
        )
        for model, polarity in model_raw.items()
    }
    if not model_polarities or any(
        polarity not in _POLARITIES for polarity in model_polarities.values()
    ):
        raise ValueError("layout model polarity must be nmos or pmos")
    layer_raw = _table(generation.get("layers"), "layout.generation.layers")
    layers = {
        _identifier(role, "layout layer role"): _identifier(
            value, f"layout.generation.layers.{role}"
        )
        for role, value in layer_raw.items()
    }
    if missing := _REQUIRED_LAYERS - set(layers):
        raise ValueError(f"layout layer roles are missing: {sorted(missing)}")
    via_raw = _table(generation.get("vias"), "layout.generation.vias")
    vias: dict[str, ViaInterface] = {}
    for raw_role, value in via_raw.items():
        role = _identifier(raw_role, "layout via role")
        item = _table(value, f"layout.generation.vias.{role}")
        _reject_unknown(
            item,
            {"definition", "landing_half_sizes"},
            f"layout.generation.vias.{role}",
        )
        landings_raw = _table(
            item.get("landing_half_sizes"),
            f"layout.generation.vias.{role}.landing_half_sizes",
        )
        landings: dict[str, tuple[int, int]] = {}
        for layer_role, size in landings_raw.items():
            if layer_role not in layers:
                raise ValueError(f"via {role} references unknown layer {layer_role}")
            if (
                not isinstance(size, list)
                or len(size) != 2
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, int)
                    or item <= 0
                    for item in size
                )
            ):
                raise ValueError(f"via {role} landing {layer_role} must be positive")
            landings[layer_role] = size[0], size[1]
        vias[role] = ViaInterface(
            _identifier(item.get("definition"), f"via {role} definition"),
            landings,
        )
    if missing := _REQUIRED_VIAS - set(vias):
        raise ValueError(f"layout via roles are missing: {sorted(missing)}")
    pcell_raw = _table(generation.get("mos_pcell"), "layout.generation.mos_pcell")
    _reject_unknown(
        pcell_raw,
        {
            "length_parameter",
            "width_parameter",
            "gate_contact_selection_parameter",
            "gate_contact_parameters",
            "polarity_parameters",
        },
        "layout.generation.mos_pcell",
    )
    polarity_raw = _table(
        pcell_raw.get("polarity_parameters", {}), "mos_pcell.polarity_parameters"
    )
    polarity_parameters = {
        polarity: _parameter_list(value, f"polarity_parameters.{polarity}")
        for polarity, value in polarity_raw.items()
    }
    if any(polarity not in _POLARITIES for polarity in polarity_parameters):
        raise ValueError("mos_pcell polarity key must be nmos or pmos")
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
            "mos_pcell.gate_contact_parameters",
        ),
        polarity_parameters=polarity_parameters,
    )
    return LayoutGenerationConfig(
        LayoutTechnologyInterface(model_polarities, layers, vias, mos_pcell),
        geometry,
    )


def _integer_map(value: object, field: str) -> Mapping[str, int]:
    raw = _table(value, field)
    result: dict[str, int] = {}
    for name, item in raw.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(item, bool)
            or not isinstance(item, int)
        ):
            raise ValueError(f"{field} must map names to integers")
        result[name] = item
    return result


def _load_layout(
    layout_path: Path,
    layout_raw: Mapping[str, Any],
    verification_path: Path,
    verification_raw: Mapping[str, Any],
    *,
    root: Path,
    owner: str,
    asset_root: Path,
) -> LayoutPdkConfig:
    _reject_unknown(
        layout_raw,
        _HEADER_FIELDS | {"dbu_per_micron", "generation"},
        "platform layout contract",
    )
    _reject_unknown(
        verification_raw,
        _HEADER_FIELDS
        | {
            "layermap",
            "drc_deck",
            "lvs_deck",
            "qrc_tech_file",
            "xstream_flatten_pcells",
            "xstream_suppressed_warnings",
            "xstream_bin",
            "calibre_bin",
            "drc_profile",
        },
        "platform verification contract",
    )
    dbu = layout_raw.get("dbu_per_micron")
    if isinstance(dbu, bool) or not isinstance(dbu, int) or dbu <= 0:
        raise ValueError("layout.dbu_per_micron must be a positive integer")
    generation = _load_generation(layout_path, layout_raw, root=root, owner=owner)
    xstream_flatten = verification_raw.get("xstream_flatten_pcells", True)
    if not isinstance(xstream_flatten, bool):
        raise ValueError("verification.xstream_flatten_pcells must be boolean")
    warnings = _strings(
        verification_raw.get("xstream_suppressed_warnings", []),
        "verification.xstream_suppressed_warnings",
    )
    if any(
        not warning.startswith("XSTRM-")
        or not warning.removeprefix("XSTRM-").isdigit()
        for warning in warnings
    ):
        raise ValueError("xstream warnings must use XSTRM-<number> identities")
    drc_raw = _table(verification_raw.get("drc_profile", {}), "drc_profile")
    _reject_unknown(
        drc_raw,
        {"disabled_defines", "configuration_warnings", "waiver_layers"},
        "drc_profile",
    )
    return LayoutPdkConfig(
        configuration_sha256="",
        layout_path=layout_path,
        verification_path=verification_path,
        dbu_per_micron=dbu,
        layermap=_required_file(asset_root, verification_raw.get("layermap"), "layermap"),
        drc_deck=_required_file(asset_root, verification_raw.get("drc_deck"), "drc_deck"),
        lvs_deck=_required_file(asset_root, verification_raw.get("lvs_deck"), "lvs_deck"),
        qrc_tech_file=_required_file(
            asset_root, verification_raw.get("qrc_tech_file"), "qrc_tech_file"
        ),
        drc_disabled_defines=_integer_map(
            drc_raw.get("disabled_defines", {}), "drc_profile.disabled_defines"
        ),
        drc_configuration_warnings=_strings(
            drc_raw.get("configuration_warnings", []),
            "drc_profile.configuration_warnings",
        ),
        drc_waiver_layers=_strings(
            drc_raw.get("waiver_layers", []), "drc_profile.waiver_layers"
        ),
        generation=generation,
        xstream_flatten_pcells=xstream_flatten,
        xstream_suppressed_warnings=warnings,
        xstream_bin=_optional_executable(
            asset_root, verification_raw.get("xstream_bin"), "xstream_bin"
        ),
        calibre_bin=_optional_executable(
            asset_root, verification_raw.get("calibre_bin"), "calibre_bin"
        ),
    )


def _source_digest(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_platform(context: ProjectContext, key: str) -> PdkConfig:
    """Resolve and validate one platform without leaking repository layout."""

    if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
        raise ValueError("platform key contains unsupported characters")
    root = context.project_root
    catalog_path = context.catalog("platform")
    if not catalog_path.is_file() or not catalog_path.is_relative_to(root):
        raise ValueError("platform catalog must be a project-owned file")
    catalog = read_toml(catalog_path)
    require_config_header(
        catalog,
        catalog_path,
        contract_kind="platform-catalog",
        path_scope="repository",
    )
    _reject_unknown(
        catalog,
        _HEADER_FIELDS | {"platforms"},
        "platform catalog",
    )
    platforms = _table(catalog.get("platforms"), "platform catalog platforms")
    try:
        manifest_value = platforms[key]
    except KeyError as exc:
        raise ValueError(f"platform catalog has no {key!r} entry") from exc
    manifest = _safe_relative(
        catalog_path.parent,
        manifest_value,
        f"platforms.{key}",
        root=root,
    )
    raw = read_toml(manifest)
    header = require_config_header(
        raw,
        manifest,
        contract_kind="platform-definition",
        path_scope="platform",
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"key", "name", "installation", "contracts"},
        "platform definition",
    )
    if raw.get("key") != key:
        raise ValueError(f"platform manifest key must be {key!r}")
    installation = _table(raw.get("installation", {}), "platform.installation")
    _reject_unknown(
        installation,
        {"root_environment", "package_root"},
        "platform.installation",
    )
    root_environment = installation.get("root_environment")
    package_root_value = installation.get("package_root")
    if (root_environment is None) != (package_root_value is None):
        raise ValueError(
            "platform installation root_environment and package_root must be paired"
        )
    asset_root: Path
    if root_environment is None:
        asset_root = manifest.parent
    else:
        if (
            not isinstance(root_environment, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", root_environment) is None
        ):
            raise ValueError("platform root_environment must be an environment name")
        package_root = Path(_text(package_root_value, "platform package_root"))
        if package_root.is_absolute() or ".." in package_root.parts:
            raise ValueError("platform package_root must be a safe relative path")
        installation_root = os.environ.get(root_environment)
        if not installation_root:
            raise ValueError(f"platform installation root is unset: {root_environment}")
        asset_root = (
            Path(installation_root).expanduser() / package_root
        ).resolve()
    contracts = _table(raw.get("contracts"), "platform.contracts")
    allowed = {"simulation", "oa", "layout", "verification"}
    if set(contracts) - allowed or not {"simulation", "oa"}.issubset(contracts):
        raise ValueError(
            "platform contracts must declare simulation and oa, with optional "
            "layout and verification"
        )
    if ("layout" in contracts) != ("verification" in contracts):
        raise ValueError("platform layout and verification contracts must be paired")
    simulation_contract = _contract(
        manifest, contracts, "simulation", root=root, owner=header.owner
    )
    oa_contract = _contract(manifest, contracts, "oa", root=root, owner=header.owner)
    assert simulation_contract is not None and oa_contract is not None
    simulation_path, simulation_raw = simulation_contract
    oa_path, oa_raw = oa_contract
    simulation = _load_simulation(
        simulation_path, simulation_raw, asset_root=asset_root
    )
    oa = _load_oa(oa_path, oa_raw)
    source_paths: list[Path] = [catalog_path, manifest, simulation_path, oa_path]
    layout: LayoutPdkConfig | None = None
    if "layout" in contracts:
        layout_contract = _contract(
            manifest, contracts, "layout", root=root, owner=header.owner
        )
        verification_contract = _contract(
            manifest, contracts, "verification", root=root, owner=header.owner
        )
        assert layout_contract is not None and verification_contract is not None
        layout_path, layout_raw = layout_contract
        verification_path, verification_raw = verification_contract
        layout = _load_layout(
            layout_path,
            layout_raw,
            verification_path,
            verification_raw,
            root=root,
            owner=header.owner,
            asset_root=asset_root,
        )
        source_paths.extend(
            (layout_path, verification_path, layout.generation.geometry.path)
        )
    sources = tuple(source_paths)
    source_sha256 = _source_digest(root, sources)
    if layout is not None:
        layout_source_paths = (
            catalog_path,
            manifest,
            oa_path,
            layout.layout_path,
            layout.verification_path,
            layout.generation.geometry.path,
        )
        layout = replace(
            layout,
            configuration_sha256=_source_digest(root, layout_source_paths),
        )
    return PdkConfig(
        key=key,
        path=manifest,
        owner=header.owner,
        name=_text(raw.get("name", key), "platform.name"),
        simulation=simulation,
        oa=oa,
        layout=layout,
        source_paths=sources,
        source_sha256=source_sha256,
        asset_root=asset_root,
        installation_root_environment=root_environment,
    )
