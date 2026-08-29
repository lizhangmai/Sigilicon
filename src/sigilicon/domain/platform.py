"""Resolved project platform contracts behind one deep loading interface."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from sigilicon.domain.config_contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    read_toml,
    require_config_header,
)
from sigilicon.domain.repository import Project


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
class OaLayerPurposeMapping:
    """One logical physical layer's explicit OA layer-purpose mapping."""

    layer: str
    drawing_purpose: str
    pin_purpose: str
    blockage_purpose: str


@dataclass(frozen=True)
class OaMaterializationMapping:
    """Atomic logical-layer and via mapping owned by a platform contract."""

    layers: Mapping[str, OaLayerPurposeMapping]
    vias: Mapping[str, str]


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
    oa_materialization: OaMaterializationMapping | None = None


@dataclass(frozen=True)
class PdkConfig:
    """Fully resolved platform and its validated operation source snapshot."""

    key: str
    path: Path
    owner: str
    name: str
    simulation: SimulationPlatformConfig
    oa: OaPlatformConfig
    layout: LayoutPdkConfig | None
    source_paths: tuple[Path, ...]
    catalog_document: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    asset_root: Path | None = None
    installation_root_environment: str | None = None
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class PlatformCatalogSnapshot:
    """One validated project platform catalog read for an operation."""

    path: Path
    project_root: Path
    owner: str
    manifests: Mapping[str, Path]
    document: Mapping[str, Any]

    def manifest(self, key: str) -> Path:
        try:
            return self.manifests[key]
        except KeyError as exc:
            raise ValueError(f"platform catalog has no {key!r} entry") from exc


_PLATFORM_INVENTORY_AUTHORITY = object()


class PlatformInventory(Mapping[str, PdkConfig]):
    """Validated platform set trusted only within one repository operation.

    Public and standalone APIs continue to accept a ``PdkConfig`` and validate
    its complete source identity. Repository workflows use this immutable
    capability after loading the catalog and every selected platform once.
    """

    __slots__ = ("_catalog", "_platforms", "_project")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        catalog: PlatformCatalogSnapshot,
        platforms: Mapping[str, PdkConfig],
    ) -> None:
        if _authority is not _PLATFORM_INVENTORY_AUTHORITY:
            raise ValueError("platform inventory must be built by its loader")
        if (
            catalog.project_root != project.project_root
            or catalog.path != project.catalog("platform")
        ):
            raise ValueError("platform inventory catalog identity drift")
        selected = dict(platforms)
        if set(selected) != set(catalog.manifests):
            raise ValueError("platform inventory does not cover its complete catalog")
        for key, platform in selected.items():
            if (
                platform.key != key
                or platform.path != catalog.manifest(key)
                or platform.catalog_document != catalog.document
                or not platform.source_paths
                or platform.source_paths[0] != catalog.path
                or set(platform.source_documents) != set(platform.source_paths[1:])
                or any(
                    not is_frozen_toml_document(document)
                    for document in platform.source_documents.values()
                )
            ):
                raise ValueError("platform inventory identity drift")
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_platforms", MappingProxyType(selected))

    @property
    def project(self) -> Project:
        return self._project

    @property
    def catalog(self) -> PlatformCatalogSnapshot:
        return self._catalog

    @property
    def platforms(self) -> Mapping[str, PdkConfig]:
        return self._platforms

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("platform inventory is immutable")

    def __getitem__(self, key: str) -> PdkConfig:
        return self.platforms[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.platforms)

    def __len__(self) -> int:
        return len(self.platforms)

    def resolve_catalog(self, context: Project) -> PlatformCatalogSnapshot:
        """Return the catalog after a cheap operation-identity check."""

        if (
            context is not self.project
            or self.catalog.project_root != context.project_root
            or self.catalog.path != context.catalog("platform")
        ):
            raise ValueError("platform inventory belongs to a different operation")
        return self.catalog

    def resolve(self, context: Project, key: str) -> PdkConfig:
        """Select one already-validated platform for this exact operation."""

        self.resolve_catalog(context)
        try:
            platform = self.platforms[key]
        except KeyError as exc:
            raise ValueError(f"platform inventory has no {key!r} entry") from exc
        if (
            self.catalog.manifest(key) != platform.path
            or platform.key != key
            or not platform.source_paths
            or platform.source_paths[0] != self.catalog.path
        ):
            raise ValueError("platform inventory identity drift")
        return platform


PlatformSnapshot = PdkConfig | PlatformInventory


def resolve_platform(
    context: Project,
    key: str,
    *,
    snapshot: PdkConfig | None = None,
) -> PdkConfig:
    """Load a platform or validate one caller-owned plan snapshot."""

    if snapshot is None:
        return load_platform(context, key)
    if snapshot.key != key:
        raise ValueError(
            f"platform snapshot {snapshot.key!r} disagrees with requested key {key!r}"
        )
    root = context.project_root
    catalog_path = context.catalog("platform")
    if (
        not isinstance(snapshot.catalog_document, Mapping)
        or not snapshot.catalog_document
    ):
        raise ValueError("platform snapshot omits its platform catalog")
    catalog_root, selected_catalog_path, _catalog_owner, platforms = (
        _platform_catalog_document(
            context,
            snapshot.catalog_document,
        )
    )
    try:
        manifest_value = platforms[key]
    except KeyError as exc:
        raise ValueError(f"platform catalog has no {key!r} entry") from exc
    selected_manifest = _safe_relative(
        selected_catalog_path.parent,
        manifest_value,
        f"platforms.{key}",
        root=catalog_root,
    )
    if (
        selected_catalog_path != catalog_path
        or selected_manifest != snapshot.path
        or not snapshot.source_paths
        or snapshot.source_paths[0] != catalog_path
    ):
        raise ValueError("platform snapshot belongs to a different project catalog")
    if snapshot.path not in snapshot.source_paths:
        raise ValueError("platform snapshot manifest is absent from its source identity")
    if any(
        not isinstance(path, Path)
        or path != path.resolve()
        or not path.is_relative_to(root)
        or not path.is_file()
        for path in snapshot.source_paths
    ):
        raise ValueError("platform snapshot source identity drift")
    document_paths = set(snapshot.source_documents)
    if not document_paths:
        raise ValueError("platform snapshot source document identity drift")
    if document_paths:
        if document_paths != set(snapshot.source_paths[1:]) or any(
            path != path.resolve() or not path.is_relative_to(root)
            for path in document_paths
        ):
            raise ValueError("platform snapshot source document identity drift")
        manifest_document = snapshot.source_documents.get(snapshot.path)
        if not isinstance(manifest_document, Mapping):
            raise ValueError("platform snapshot source document drift")
        require_config_header(
            manifest_document,
            snapshot.path,
            contract_kind="platform-definition",
            path_scope="platform",
            owner=snapshot.owner,
        )
        _reject_unknown(
            manifest_document,
            _HEADER_FIELDS | {"key", "name", "installation", "contracts"},
            "platform definition",
        )
        asset_root, root_environment = _platform_asset_root(
            snapshot.path,
            manifest_document,
        )
        if (
            manifest_document.get("key") != key
            or _text(manifest_document.get("name", key), "platform.name")
            != snapshot.name
            or snapshot.asset_root != asset_root
            or snapshot.installation_root_environment != root_environment
        ):
            raise ValueError("platform identity drift")
        contracts = manifest_document.get("contracts")
        if not isinstance(contracts, Mapping):
            raise ValueError("platform snapshot source document drift")
        allowed_contracts = {"simulation", "oa", "layout", "verification"}
        if set(contracts) - allowed_contracts or not {
            "simulation",
            "oa",
        }.issubset(contracts):
            raise ValueError("platform snapshot source document drift")
        if ("layout" in contracts) != ("verification" in contracts):
            raise ValueError("platform snapshot source document drift")
        simulation_path = _safe_relative(
            snapshot.path.parent,
            contracts.get("simulation"),
            "platform.contracts.simulation",
            root=root,
        )
        oa_path = _safe_relative(
            snapshot.path.parent,
            contracts.get("oa"),
            "platform.contracts.oa",
            root=root,
        )
        simulation_document = snapshot.source_documents.get(simulation_path)
        oa_document = snapshot.source_documents.get(oa_path)
        if not isinstance(simulation_document, Mapping) or not isinstance(
            oa_document,
            Mapping,
        ):
            raise ValueError("platform identity drift")
        require_config_header(
            simulation_document,
            simulation_path,
            contract_kind="platform-simulation",
            path_scope="platform",
            owner=snapshot.owner,
        )
        require_config_header(
            oa_document,
            oa_path,
            contract_kind="platform-oa",
            path_scope="platform",
            owner=snapshot.owner,
        )
        parsed_simulation = _load_simulation(
            simulation_path,
            simulation_document,
            asset_root=asset_root,
        )
        parsed_oa = _load_oa(oa_path, oa_document)
        if parsed_simulation != snapshot.simulation or parsed_oa != snapshot.oa:
            raise ValueError("platform identity drift")
        has_layout_contracts = "layout" in contracts
        if (snapshot.layout is None) != (not has_layout_contracts):
            raise ValueError("platform identity drift")
        contract_paths = [simulation_path, oa_path]
        if snapshot.layout is not None:
            layout_path = _safe_relative(
                snapshot.path.parent,
                contracts.get("layout"),
                "platform.contracts.layout",
                root=root,
            )
            verification_path = _safe_relative(
                snapshot.path.parent,
                contracts.get("verification"),
                "platform.contracts.verification",
                root=root,
            )
            layout_document = snapshot.source_documents.get(layout_path)
            verification_document = snapshot.source_documents.get(verification_path)
            if (
                snapshot.layout.layout_path != layout_path
                or snapshot.layout.verification_path != verification_path
                or not isinstance(layout_document, Mapping)
                or not isinstance(verification_document, Mapping)
            ):
                raise ValueError("platform identity drift")
            require_config_header(
                layout_document,
                layout_path,
                contract_kind="platform-layout",
                path_scope="platform",
                owner=snapshot.owner,
            )
            require_config_header(
                verification_document,
                verification_path,
                contract_kind="platform-verification",
                path_scope="platform",
                owner=snapshot.owner,
            )
            parsed_layout = _load_layout(
                layout_path,
                layout_document,
                verification_path,
                verification_document,
                asset_root=asset_root,
            )
            if parsed_layout != snapshot.layout:
                raise ValueError("platform identity drift")
            contract_paths.extend((layout_path, verification_path))
        if snapshot.source_paths != (
            catalog_path,
            snapshot.path,
            *contract_paths,
        ):
            raise ValueError("platform snapshot source identity drift")
    return snapshot


def resolve_platform_snapshot(
    context: Project,
    key: str,
    *,
    snapshot: PlatformSnapshot | None = None,
) -> PdkConfig:
    """Resolve a public snapshot or select an operation-trusted inventory."""

    if isinstance(snapshot, PlatformInventory):
        return snapshot.resolve(context, key)
    return resolve_platform(context, key, snapshot=snapshot)


def resolve_platform_catalog(
    context: Project,
    *,
    snapshot: PlatformCatalogSnapshot | None = None,
) -> PlatformCatalogSnapshot:
    """Load a platform catalog or validate one operation-owned snapshot."""

    if snapshot is None:
        return load_platform_catalog(context)
    validated = parse_platform_catalog(context, snapshot.document)
    if (
        snapshot.project_root != validated.project_root
        or snapshot.path != validated.path
    ):
        raise ValueError("platform catalog snapshot belongs to a different project")
    if snapshot.owner != validated.owner or snapshot.manifests != validated.manifests:
        raise ValueError("platform catalog snapshot identity drift")
    return snapshot


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
    if not isinstance(value, (list, tuple)) or (not value and not empty):
        raise ValueError(f"{field} must be a string array")
    result = tuple(_identifier(item, f"{field}[]") for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _strings(value: object, field: str, *, empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not empty) or any(
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


def _platform_asset_root(
    manifest: Path,
    raw: Mapping[str, Any],
) -> tuple[Path, str | None]:
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
    if root_environment is None:
        return manifest.parent, None
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
    asset_root = (Path(installation_root).expanduser() / package_root).resolve()
    return asset_root, root_environment


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
    return SimulationPlatformConfig(
        path,
        default_name,
        MappingProxyType(model_sets),
    )


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
        primitive_subcircuits=MappingProxyType(primitive_subcircuits),
    )


def _parse_oa_materialization(
    value: object,
) -> OaMaterializationMapping | None:
    if value is None:
        return None
    raw = _table(value, "layout.oa_materialization")
    _reject_unknown(raw, {"layers", "vias"}, "layout.oa_materialization")
    layers_raw = _table(raw.get("layers"), "layout.oa_materialization.layers")
    layers: dict[str, OaLayerPurposeMapping] = {}
    for logical_value, item_value in layers_raw.items():
        logical = _text(logical_value, "layout.oa_materialization.layers key")
        item = _table(
            item_value,
            f"layout.oa_materialization.layers.{logical}",
        )
        _reject_unknown(
            item,
            {
                "layer",
                "drawing_purpose",
                "pin_purpose",
                "blockage_purpose",
            },
            f"layout.oa_materialization.layers.{logical}",
        )
        layers[logical] = OaLayerPurposeMapping(
            layer=_text(item.get("layer"), f"layers.{logical}.layer"),
            drawing_purpose=_text(
                item.get("drawing_purpose"),
                f"layers.{logical}.drawing_purpose",
            ),
            pin_purpose=_text(
                item.get("pin_purpose"),
                f"layers.{logical}.pin_purpose",
            ),
            blockage_purpose=_text(
                item.get("blockage_purpose"),
                f"layers.{logical}.blockage_purpose",
            ),
        )
    vias_raw = _table(raw.get("vias"), "layout.oa_materialization.vias")
    vias = {
        _text(logical_value, "layout.oa_materialization.vias key"): _identifier(
            via_value,
            f"layout.oa_materialization.vias.{logical_value}",
        )
        for logical_value, via_value in vias_raw.items()
    }
    return OaMaterializationMapping(
        layers=MappingProxyType(layers),
        vias=MappingProxyType(vias),
    )


def load_oa_materialization_mapping(
    path: Path,
) -> tuple[int, OaMaterializationMapping]:
    """Load one atomic platform layout contract for an OA backend."""

    contract_path = Path(path).resolve()
    raw = read_toml(contract_path)
    require_config_header(
        raw,
        contract_path,
        contract_kind="platform-layout",
        path_scope="platform",
    )
    _reject_unknown(
        raw,
        _HEADER_FIELDS | {"dbu_per_micron", "oa_materialization", "custom_layout"},
        "platform layout contract",
    )
    dbu = raw.get("dbu_per_micron")
    if isinstance(dbu, bool) or not isinstance(dbu, int) or dbu <= 0:
        raise ValueError("layout.dbu_per_micron must be a positive integer")
    mapping = _parse_oa_materialization(raw.get("oa_materialization"))
    if mapping is None:
        raise ValueError("platform layout contract omits oa_materialization")
    return dbu, mapping


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
        _HEADER_FIELDS | {"dbu_per_micron", "oa_materialization", "custom_layout"},
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
    oa_materialization = _parse_oa_materialization(
        layout_raw.get("oa_materialization")
    )
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
        oa_materialization=oa_materialization,
    )


def _platform_catalog_document(
    context: Project,
    document: Mapping[str, Any],
) -> tuple[Path, Path, str, Mapping[str, Any]]:
    root = context.project_root
    catalog_path = context.catalog("platform")
    if not catalog_path.is_file() or not catalog_path.is_relative_to(root):
        raise ValueError("platform catalog must be a project-owned file")
    header = require_config_header(
        document,
        catalog_path,
        contract_kind="platform-catalog",
        path_scope="repository",
    )
    _reject_unknown(
        document,
        _HEADER_FIELDS | {"platforms"},
        "platform catalog",
    )
    platforms = _table(document.get("platforms"), "platform catalog platforms")
    return root, catalog_path, header.owner, platforms


def parse_platform_catalog(
    context: Project,
    document: Mapping[str, Any],
) -> PlatformCatalogSnapshot:
    """Validate an already read canonical platform catalog document."""

    root, catalog_path, owner, platforms = _platform_catalog_document(
        context,
        document,
    )
    manifests: dict[str, Path] = {}
    for key, value in platforms.items():
        if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
            raise ValueError("platform catalog keys contain unsupported characters")
        manifests[key] = _safe_relative(
            catalog_path.parent,
            value,
            f"platforms.{key}",
            root=root,
        )
    return PlatformCatalogSnapshot(
        path=catalog_path,
        project_root=root,
        owner=owner,
        manifests=MappingProxyType(manifests),
        document=freeze_toml_document(document),
    )


def load_platform_catalog(context: Project) -> PlatformCatalogSnapshot:
    """Read and validate the project's canonical platform catalog once."""

    catalog_path = context.catalog("platform")
    return parse_platform_catalog(context, read_toml(catalog_path))


def load_platform(
    context: Project,
    key: str,
    *,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PdkConfig:
    """Resolve and validate one platform without leaking repository layout."""

    if not isinstance(key, str) or _PLATFORM_KEY.fullmatch(key) is None:
        raise ValueError("platform key contains unsupported characters")
    if catalog is None:
        catalog_document = read_toml(context.catalog("platform"))
        root, catalog_path, _owner, platforms = _platform_catalog_document(
            context,
            catalog_document,
        )
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
    else:
        catalog_snapshot = resolve_platform_catalog(context, snapshot=catalog)
        catalog_document = catalog_snapshot.document
        root = catalog_snapshot.project_root
        catalog_path = catalog_snapshot.path
        manifest = catalog_snapshot.manifest(key)
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
    asset_root, root_environment = _platform_asset_root(manifest, raw)
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
    source_paths: list[Path] = [
        catalog_path,
        manifest,
        simulation_path,
        oa_path,
    ]
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
    source_documents = {
        manifest: raw,
        simulation_path: simulation_raw,
        oa_path: oa_raw,
    }
    if layout is not None:
        source_documents[layout.layout_path] = layout_raw
        source_documents[layout.verification_path] = verification_raw
    return PdkConfig(
        key=key,
        path=manifest,
        owner=header.owner,
        name=_text(raw.get("name", key), "platform.name"),
        simulation=simulation,
        oa=oa,
        layout=layout,
        source_paths=sources,
        catalog_document=freeze_toml_document(catalog_document),
        source_documents=MappingProxyType(
            {
                path: freeze_toml_document(document)
                for path, document in source_documents.items()
            }
        ),
        asset_root=asset_root,
        installation_root_environment=root_environment,
    )


def load_platform_inventory(
    context: Project,
    *,
    catalog: PlatformCatalogSnapshot | None = None,
) -> PlatformInventory:
    """Load the complete project platform set once for one operation."""

    selected_catalog = (
        load_platform_catalog(context)
        if catalog is None
        else resolve_platform_catalog(context, snapshot=catalog)
    )
    platforms = {
        key: load_platform(context, key, catalog=selected_catalog)
        for key in selected_catalog.manifests
    }
    return PlatformInventory(
        _authority=_PLATFORM_INVENTORY_AUTHORITY,
        project=context,
        catalog=selected_catalog,
        platforms=MappingProxyType(platforms),
    )
