"""Resolved project platform contracts behind one deep loading interface."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sigilicon.domain.config_contracts import read_toml, require_config_header
from sigilicon.domain.repository import RepositoryContext


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_PLATFORM_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
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
class OaPlatformConfig:
    """OA technology and primitive facts owned by the project platform."""

    path: Path
    technology_library: str
    reference_libraries: tuple[str, ...]
    primitive_subcircuits: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class LayoutPdkConfig:
    """Resolved layout and physical-verification platform capability."""

    layout_path: Path
    verification_path: Path
    dbu_per_micron: int
    layermap: Path
    drc_deck: Path
    lvs_deck: Path
    qrc_tech_file: Path | None
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


def _load_oa(path: Path, raw: Mapping[str, Any]) -> OaPlatformConfig:
    _reject_unknown(
        raw,
        _HEADER_FIELDS
        | {
            "technology_library",
            "reference_libraries",
            "primitive_subcircuits",
        },
        "platform OA contract",
    )
    primitive_raw = _table(raw.get("primitive_subcircuits", {}), "primitive_subcircuits")
    primitive_subcircuits = {
        _identifier(master, "primitive_subcircuits key"): _names(
            terminals, f"primitive_subcircuits.{master}"
        )
        for master, terminals in primitive_raw.items()
    }
    return OaPlatformConfig(
        path=path,
        technology_library=_identifier(
            raw.get("technology_library"), "technology_library"
        ),
        reference_libraries=_names(raw.get("reference_libraries"), "reference_libraries"),
        primitive_subcircuits=primitive_subcircuits,
    )


def _load_layout(
    layout_path: Path,
    layout_raw: Mapping[str, Any],
    verification_path: Path,
    verification_raw: Mapping[str, Any],
    *,
    asset_root: Path,
) -> LayoutPdkConfig:
    _reject_unknown(
        layout_raw,
        _HEADER_FIELDS | {"dbu_per_micron"},
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
        },
        "platform verification contract",
    )
    dbu = layout_raw.get("dbu_per_micron")
    if isinstance(dbu, bool) or not isinstance(dbu, int) or dbu <= 0:
        raise ValueError("layout.dbu_per_micron must be a positive integer")
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
    return LayoutPdkConfig(
        layout_path=layout_path,
        verification_path=verification_path,
        dbu_per_micron=dbu,
        layermap=_required_file(asset_root, verification_raw.get("layermap"), "layermap"),
        drc_deck=_required_file(asset_root, verification_raw.get("drc_deck"), "drc_deck"),
        lvs_deck=_required_file(asset_root, verification_raw.get("lvs_deck"), "lvs_deck"),
        qrc_tech_file=(
            _required_file(
                asset_root,
                verification_raw["qrc_tech_file"],
                "qrc_tech_file",
            )
            if "qrc_tech_file" in verification_raw
            else None
        ),
        xstream_flatten_pcells=xstream_flatten,
        xstream_suppressed_warnings=warnings,
        xstream_bin=_optional_executable(
            asset_root, verification_raw.get("xstream_bin"), "xstream_bin"
        ),
        calibre_bin=_optional_executable(
            asset_root, verification_raw.get("calibre_bin"), "calibre_bin"
        ),
    )


def load_platform(context: RepositoryContext, key: str) -> PdkConfig:
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
            asset_root=asset_root,
        )
        source_paths.extend((layout_path, verification_path))
    sources = tuple(source_paths)
    return PdkConfig(
        key=key,
        path=manifest,
        owner=header.owner,
        name=_text(raw.get("name", key), "platform.name"),
        simulation=simulation,
        oa=oa,
        layout=layout,
        source_paths=sources,
        asset_root=asset_root,
        installation_root_environment=root_environment,
    )
