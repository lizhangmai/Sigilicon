"""Canonical OA ownership roots and library assembly contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.domain.config_contracts import (
    freeze_toml_document,
    is_frozen_toml_document,
    require_config_header,
)
from sigilicon.domain.physical_verification import (
    PhysicalVerificationPolicy,
    parse_physical_verification_policy,
)
from sigilicon.domain.repository import Project

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_VIEW_KINDS = {
    "spectre_netlist",
    "spectre_model",
    "schematic",
    "symbol",
    "layout",
    "veriloga",
    "system_verilog",
    "skill",
    "config",
    "maestro",
}
_CELL_ROLES = {"design", "model", "testbench"}
_NATIVE_VIEW_NAMES = {
    "spectre_netlist": "netlist",
    "spectre_model": "spectre",
    "schematic": "schematic",
    "symbol": "symbol",
    "veriloga": "veriloga",
    "system_verilog": "systemVerilog",
    "skill": "measurement",
    "config": "config",
    "maestro": "maestro",
}
_SOURCE_MANIFEST_FIELDS = {
    "schema",
    "contract_kind",
    "path_scope",
    "owner",
    "cell_roots",
}
_ASSEMBLY_FIELDS = {
    "schema",
    "contract_kind",
    "path_scope",
    "name",
    "pdk",
    "additional_source_manifests",
    "primitive_masters",
    "physical_verification",
}
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


@dataclass(frozen=True, order=True)
class OAViewReference:
    """One exact library-local cell/view dependency."""

    cell: str
    view: str


@dataclass(frozen=True)
class OACellViewSource:
    """Canonical source and materializer kind for one OA view."""

    name: str
    kind: str
    source: Path
    dependencies: tuple[OAViewReference, ...]


@dataclass(frozen=True)
class OACellSource:
    """Canonical source and explicit view set owned by one IP."""

    owner: str
    source_manifest_path: Path
    manifest_path: Path
    directory: Path
    cell: str
    role: str
    canonical_source: Path
    views: tuple[OACellViewSource, ...]

    @property
    def design_spec(self) -> Path | None:
        """Return the unique design spec used by schematic generation, if any."""

        values = {
            view.source
            for view in self.views
            if view.kind in {"schematic", "symbol"}
            and view.source.name == "design.toml"
        }
        if len(values) > 1:
            raise ValueError(f"{self.cell} views disagree on their design spec")
        return next(iter(values), None)

    @property
    def layout_specs(self) -> tuple[Path, ...]:
        return tuple(view.source for view in self.views if view.kind == "layout")

    def view(self, name: str) -> OACellViewSource:
        try:
            return next(view for view in self.views if view.name == name)
        except StopIteration as exc:
            raise KeyError(f"{self.cell} does not declare OA view {name}") from exc


@dataclass(frozen=True)
class OASourceRoot:
    """One IP-owned collection of OA cell sources."""

    owner: str
    manifest_path: Path
    directory: Path
    cell_roots: tuple[Path, ...]
    cells: tuple[OACellSource, ...]
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class OALibrarySource:
    """Assembly contract for one generated OA library."""

    manifest_path: Path
    project: Project
    name: str
    pdk: str
    workspace_template: Path
    oa_library: Path
    primitive_masters: tuple[str, ...]
    physical_verification: PhysicalVerificationPolicy | None
    source_roots: tuple[OASourceRoot, ...]
    cells: tuple[OACellSource, ...]
    source_documents: Mapping[Path, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )

    @property
    def project_root(self) -> Path:
        return self.project.project_root


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", value
    ):
        raise ValueError(f"{field} must be a non-empty token")
    return value


def _strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not allow_empty):
        qualifier = "" if allow_empty else " non-empty"
        raise ValueError(f"{field} must be a{qualifier} string array")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _project_path(root: Path, value: object, field: str, *, file: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty project-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must stay below the project root")
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f"{field} must stay below the project root")
    if file and not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    return result


def _cell_owned_path(directory: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty cell-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must stay inside its cell directory")
    result = (directory / relative).resolve()
    if not result.is_relative_to(directory.resolve()):
        raise ValueError(f"{field} must stay inside its cell directory")
    if not result.is_file():
        raise ValueError(f"{field} does not exist: {result}")
    return result


def _is_non_oa_verification_cell(raw: dict[str, Any]) -> bool:
    """Return whether one cell contract declares a non-OA verification cell.

    Verification is organized by circuit responsibility, so one domain root may
    contain both native OA testbench cells and RTL-only verification cells.  The
    OA assembly consumes the former and leaves the latter to their own runner.
    """

    return raw.get("contract_kind") == "verification-cell"


def _view_reference(value: str, field: str) -> OAViewReference:
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError(f"{field} must use CELL/view syntax")
    return OAViewReference(
        cell=_identifier(parts[0], f"{field}.cell"),
        view=_identifier(parts[1], f"{field}.view"),
    )


def _load_cell(
    owner: str,
    source_manifest: Path,
    directory: Path,
    raw: Mapping[str, Any],
) -> OACellSource:
    cell_manifest = directory / "cell.toml"
    unknown = set(raw) - {
        "schema",
        "contract_kind",
        "path_scope",
        "owner",
        "cell",
        "role",
        "canonical_source",
        "views",
    }
    if unknown:
        raise ValueError(
            f"unsupported cell contract fields in {cell_manifest}: {sorted(unknown)}"
        )
    header = require_config_header(
        raw,
        cell_manifest,
        contract_kind="oa-cell",
        path_scope="cell",
        owner=owner,
    )
    if header.owner != owner:
        raise ValueError(f"OA cell owner disagrees with source manifest: {cell_manifest}")
    cell = _identifier(raw.get("cell"), f"{cell_manifest}: cell")
    if cell != directory.name:
        raise ValueError(f"cell must match its directory name: {cell_manifest}")
    role = _token(raw.get("role"), f"{cell_manifest}: role")
    if role not in _CELL_ROLES:
        raise ValueError(f"unsupported OA cell role in {cell_manifest}: {role}")
    canonical_source = _cell_owned_path(
        directory,
        raw.get("canonical_source"),
        f"{cell_manifest}: canonical_source",
    )
    rows = raw.get("views")
    if not isinstance(rows, (list, tuple)) or not rows or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise ValueError(f"{cell_manifest}: views must be a non-empty array of tables")
    views: list[OACellViewSource] = []
    for index, row in enumerate(rows):
        field = f"{cell_manifest}: views[{index}]"
        unknown_view = set(row) - {"name", "kind", "source", "dependencies"}
        if unknown_view:
            raise ValueError(f"unsupported {field} fields: {sorted(unknown_view)}")
        name = _identifier(row.get("name"), f"{field}.name")
        kind = _token(row.get("kind"), f"{field}.kind")
        if kind not in _VIEW_KINDS:
            raise ValueError(f"unsupported {field}.kind: {kind}")
        native_name = _NATIVE_VIEW_NAMES.get(kind)
        if native_name is not None and name != native_name:
            raise ValueError(f"{field} kind {kind} must use OA view name {native_name}")
        source = _cell_owned_path(directory, row.get("source"), f"{field}.source")
        dependencies = tuple(
            _view_reference(value, f"{field}.dependencies[]")
            for value in _strings(
                row.get("dependencies", []),
                f"{field}.dependencies",
                allow_empty=True,
            )
        )
        views.append(
            OACellViewSource(
                name=name,
                kind=kind,
                source=source,
                dependencies=dependencies,
            )
        )
    if len({view.name for view in views}) != len(views):
        raise ValueError(f"duplicate OA view names in {cell_manifest}")
    if role == "design":
        required = {"netlist", "schematic", "symbol"}
        missing = required - {view.name for view in views}
        if missing:
            raise ValueError(f"design {cell} lacks required views: {sorted(missing)}")
    if role == "testbench" and not {
        "netlist",
        "schematic",
        "config",
        "measurement",
        "maestro",
    }.issubset(
        {view.name for view in views}
    ):
        raise ValueError(
            f"testbench {cell} must declare netlist, schematic, config, measurement, and maestro views"
        )
    return OACellSource(
        owner=owner,
        source_manifest_path=source_manifest,
        manifest_path=cell_manifest,
        directory=directory.resolve(),
        cell=cell,
        role=role,
        canonical_source=canonical_source,
        views=tuple(views),
    )


def _load_source_root(
    path: Path,
    *,
    context: Project,
    allow_assembly_fields: bool = False,
    raw: dict[str, Any] | None = None,
) -> OASourceRoot:
    raw = _read_toml(path) if raw is None else raw
    allowed = _SOURCE_MANIFEST_FIELDS | (
        _ASSEMBLY_FIELDS if allow_assembly_fields else set()
    )
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported OA source manifest fields in {path}: {sorted(unknown)}")
    require_config_header(
        raw,
        path,
        contract_kind=("oa-assembly" if allow_assembly_fields else "oa-source-root"),
        path_scope="owner",
    )
    owner = _token(raw.get("owner"), f"{path}: owner")
    repository_owner = context.require_owner(path)
    if repository_owner.name != owner:
        raise ValueError(f"OA source manifest owner disagrees with its catalog: {path}")
    directory = repository_owner.root
    root_values = _strings(raw.get("cell_roots"), f"{path}: cell_roots")
    cell_roots: list[Path] = []
    source_directories: list[Path] = []
    cell_documents: dict[Path, dict[str, Any]] = {}
    for index, value in enumerate(root_values):
        relative = Path(value)
        field = f"{path}: cell_roots[{index}]"
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ValueError(f"{field} must stay inside its owning IP")
        cell_root = (directory / relative).resolve()
        if not cell_root.is_relative_to(directory) or not cell_root.is_dir():
            raise ValueError(f"{field} must be an existing directory inside its owning IP")
        all_children = tuple(
            item
            for item in sorted(cell_root.iterdir())
            if item.is_dir()
            and not item.name.startswith(".")
            and item.name != "__pycache__"
        )
        escaping = [
            item.relative_to(directory).as_posix()
            for item in all_children
            if not item.resolve().is_relative_to(cell_root)
            or not (item / "cell.toml").resolve().is_relative_to(cell_root)
        ]
        if escaping:
            raise ValueError(
                "OA cell directory or manifest escapes its declared cell root: "
                f"{escaping}"
            )
        undeclared = [
            item.relative_to(directory).as_posix()
            for item in all_children
            if not (item / "cell.toml").is_file()
        ]
        if undeclared:
            raise ValueError(
                f"OA cell root contains directories without cell.toml: {undeclared}"
            )
        cell_documents.update(
            {item: _read_toml(item / "cell.toml") for item in all_children}
        )
        children = tuple(
            item
            for item in all_children
            if not _is_non_oa_verification_cell(cell_documents[item])
        )
        if not children:
            raise ValueError(f"OA cell root declares no cells: {cell_root}")
        cell_roots.append(cell_root)
        source_directories.extend(children)
    if len(set(source_directories)) != len(source_directories):
        raise ValueError(f"OA source manifest selects a cell directory more than once: {path}")
    cells = tuple(
        _load_cell(owner, path, item, cell_documents[item])
        for item in source_directories
    )
    if not cells:
        raise ValueError(f"OA source manifest declares no cells: {path}")
    source_documents = {path.resolve(): freeze_toml_document(raw)}
    source_documents.update(
        {
            (directory / "cell.toml").resolve(): freeze_toml_document(document)
            for directory, document in cell_documents.items()
        }
    )
    return OASourceRoot(
        owner=owner,
        manifest_path=path,
        directory=directory,
        cell_roots=tuple(cell_roots),
        cells=cells,
        source_documents=MappingProxyType(source_documents),
    )


def load_oa_library_source(
    path: Path,
    *,
    project: Project,
) -> OALibrarySource:
    """Load one OA assembly and all explicitly selected IP source roots."""

    manifest_path = path.resolve()
    context = project
    root = context.project_root
    repository_owner = context.require_owner(manifest_path)
    raw = _read_toml(manifest_path)
    allowed = _ASSEMBLY_FIELDS | _SOURCE_MANIFEST_FIELDS
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            f"unsupported OA assembly fields in {manifest_path}: {sorted(unknown)}"
        )
    require_config_header(
        raw,
        manifest_path,
        contract_kind="oa-assembly",
        path_scope="owner",
    )
    name = _identifier(raw.get("name"), "name")
    pdk = _identifier(raw.get("pdk"), "pdk")
    assembly_owner = _token(raw.get("owner"), f"{manifest_path}: owner")
    if assembly_owner != repository_owner.name:
        raise ValueError("OA assembly owner disagrees with its repository catalog")
    workspace_template = context.workspace_root
    if not workspace_template.is_dir():
        raise ValueError("project workspace root does not exist")
    oa_library = workspace_template / name
    primitive_masters = tuple(
        _identifier(value, "primitive_masters[]")
        for value in _strings(
            raw.get("primitive_masters"), "primitive_masters", allow_empty=True
        )
    )
    physical_verification_value = raw.get("physical_verification")
    physical_verification = None
    if physical_verification_value is not None:
        physical_verification_path = _project_path(
            root,
            physical_verification_value,
            "physical_verification",
            file=True,
        )
        if not physical_verification_path.is_relative_to(repository_owner.root):
            raise ValueError(
                "physical_verification must stay inside the assembly owner"
            )
        physical_verification_raw = _read_toml(physical_verification_path)
        physical_verification = parse_physical_verification_policy(
            physical_verification_path,
            physical_verification_raw,
            owner=assembly_owner,
        )
    additional_manifest_values = _strings(
        raw.get("additional_source_manifests", []),
        "additional_source_manifests",
        allow_empty=True,
    )
    additional_manifests = tuple(
        _project_path(
            root,
            value,
            "additional_source_manifests[]",
            file=True,
        )
        for value in additional_manifest_values
    )
    if manifest_path in additional_manifests:
        raise ValueError("OA assembly must not list itself as an additional source")
    source_roots = (
        _load_source_root(
            manifest_path,
            context=context,
            allow_assembly_fields=True,
            raw=raw,
        ),
        *(
            _load_source_root(source, context=context)
            for source in additional_manifests
        ),
    )
    owners = tuple(source.owner for source in source_roots)
    if len(set(owners)) != len(owners):
        raise ValueError("OA assembly contains duplicate source owners")
    cells = tuple(cell for source in source_roots for cell in source.cells)
    names = tuple(cell.cell for cell in cells)
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"OA cell names must be globally unique: {duplicates}")
    available_views = {
        OAViewReference(cell.cell, view.name)
        for cell in cells
        for view in cell.views
    }
    unresolved = sorted(
        {
            dependency
            for cell in cells
            for view in cell.views
            for dependency in view.dependencies
            if dependency not in available_views
        }
    )
    if unresolved:
        rendered = [f"{item.cell}/{item.view}" for item in unresolved]
        raise ValueError(f"OA assembly has unresolved view dependencies: {rendered}")
    source_documents: dict[Path, Mapping[str, Any]] = {}
    for source in source_roots:
        for source_path, document in source.source_documents.items():
            previous = source_documents.get(source_path)
            if previous is not None and previous != document:
                raise ValueError(
                    f"OA source documents disagree for {source_path}"
                )
            source_documents[source_path] = document
    if physical_verification is not None:
        source_documents[physical_verification.path] = physical_verification.document
    return OALibrarySource(
        manifest_path=manifest_path,
        project=context,
        name=name,
        pdk=pdk,
        workspace_template=workspace_template,
        oa_library=oa_library,
        primitive_masters=primitive_masters,
        physical_verification=physical_verification,
        source_roots=source_roots,
        cells=cells,
        source_documents=MappingProxyType(source_documents),
    )


def resolve_oa_library_source(
    path: Path,
    *,
    project: Project,
    snapshot: OALibrarySource | None = None,
) -> OALibrarySource:
    """Load an OA source or validate one caller-owned operation snapshot."""

    if snapshot is None:
        return load_oa_library_source(
            path,
            project=project,
        )
    context = project
    manifest_path = path.resolve()
    if not manifest_path.is_relative_to(context.project_root):
        raise ValueError("OA source snapshot manifest escapes the project root")
    owner = context.require_owner(manifest_path)
    if (
        snapshot.project is not context
        or snapshot.manifest_path != manifest_path
        or not snapshot.source_roots
        or snapshot.source_roots[0].manifest_path != manifest_path
        or snapshot.source_roots[0].owner != owner.name
    ):
        raise ValueError("OA source snapshot identity drift")
    expected_documents: dict[Path, Mapping[str, Any]] = {}
    expected_cells: list[OACellSource] = []
    for index, source_root in enumerate(snapshot.source_roots):
        root_owner = context.require_owner(source_root.manifest_path)
        if (
            source_root.manifest_path != source_root.manifest_path.resolve()
            or not source_root.manifest_path.is_relative_to(context.project_root)
            or source_root.owner != root_owner.name
            or source_root.directory != root_owner.root
        ):
            raise ValueError("OA source snapshot source-root identity drift")
        expected_paths = {source_root.manifest_path}
        for cell_root in source_root.cell_roots:
            if (
                cell_root != cell_root.resolve()
                or not cell_root.is_relative_to(source_root.directory)
                or not cell_root.is_dir()
            ):
                raise ValueError("OA source snapshot cell-root identity drift")
            for child in cell_root.iterdir():
                if (
                    not child.is_dir()
                    or child.name.startswith(".")
                    or child.name == "__pycache__"
                ):
                    continue
                if not child.resolve().is_relative_to(cell_root):
                    raise ValueError(
                        "OA source snapshot cell directory escapes its cell root"
                    )
                cell_manifest = (child / "cell.toml").resolve()
                if not cell_manifest.is_relative_to(cell_root):
                    raise ValueError(
                        "OA source snapshot cell manifest escapes its cell root"
                    )
                if not cell_manifest.is_file():
                    raise ValueError(
                        "OA source snapshot cell directory lacks cell.toml"
                    )
                expected_paths.add(cell_manifest)
        if set(source_root.source_documents) != expected_paths:
            raise ValueError("OA source snapshot document set drift")
        manifest_document = source_root.source_documents[source_root.manifest_path]
        allow_assembly_fields = index == 0
        allowed = _SOURCE_MANIFEST_FIELDS | (
            _ASSEMBLY_FIELDS if allow_assembly_fields else set()
        )
        if set(manifest_document) - allowed:
            raise ValueError("OA source snapshot manifest document drift")
        require_config_header(
            manifest_document,
            source_root.manifest_path,
            contract_kind=(
                "oa-assembly" if allow_assembly_fields else "oa-source-root"
            ),
            path_scope="owner",
            owner=source_root.owner,
        )
        declared_roots_list: list[Path] = []
        for value in _strings(
            manifest_document.get("cell_roots"),
            f"{source_root.manifest_path}: cell_roots",
        ):
            relative = Path(value)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative == Path(".")
            ):
                raise ValueError(
                    "OA source snapshot cell-root declaration is unsafe"
                )
            declared_root = (source_root.directory / relative).resolve()
            if (
                not declared_root.is_relative_to(source_root.directory)
                or not declared_root.is_dir()
            ):
                raise ValueError(
                    "OA source snapshot cell-root declaration drift"
                )
            declared_roots_list.append(declared_root)
        declared_roots = tuple(declared_roots_list)
        if len(set(declared_roots)) != len(declared_roots):
            raise ValueError("OA source snapshot contains duplicate cell roots")
        if declared_roots != source_root.cell_roots:
            raise ValueError("OA source snapshot cell-root declaration drift")
        if allow_assembly_fields:
            declared_name = _identifier(
                manifest_document.get("name"),
                f"{source_root.manifest_path}: name",
            )
            declared_pdk = _identifier(
                manifest_document.get("pdk"),
                f"{source_root.manifest_path}: pdk",
            )
            declared_workspace = context.workspace_root
            declared_library = declared_workspace / declared_name
            declared_primitives = tuple(
                _identifier(value, "primitive_masters[]")
                for value in _strings(
                    manifest_document.get("primitive_masters"),
                    "primitive_masters",
                    allow_empty=True,
                )
            )
            declared_additional = tuple(
                _project_path(
                    context.project_root,
                    value,
                    "additional_source_manifests[]",
                    file=True,
                )
                for value in _strings(
                    manifest_document.get("additional_source_manifests", ()),
                    "additional_source_manifests",
                    allow_empty=True,
                )
            )
            policy_value = manifest_document.get("physical_verification")
            declared_policy = (
                None
                if policy_value is None
                else _project_path(
                    context.project_root,
                    policy_value,
                    "physical_verification",
                    file=True,
                )
            )
            if (
                declared_name != snapshot.name
                or declared_pdk != snapshot.pdk
                or declared_workspace != snapshot.workspace_template
                or declared_workspace != context.workspace_root
                or not declared_workspace.is_dir()
                or declared_library != snapshot.oa_library
                or declared_library != declared_workspace / declared_name
                or declared_primitives != snapshot.primitive_masters
                or declared_additional
                != tuple(
                    item.manifest_path for item in snapshot.source_roots[1:]
                )
                or declared_policy
                != (
                    None
                    if snapshot.physical_verification is None
                    else snapshot.physical_verification.path
                )
            ):
                raise ValueError("OA source snapshot assembly document drift")
        parsed_cells: list[OACellSource] = []
        for source_path, document in source_root.source_documents.items():
            if (
                source_path != source_path.resolve()
                or not source_path.is_relative_to(source_root.directory)
                or not isinstance(document, Mapping)
            ):
                raise ValueError("OA source snapshot document identity drift")
            if source_path != source_root.manifest_path:
                if document.get("contract_kind") == "verification-cell":
                    require_config_header(
                        document,
                        source_path,
                        contract_kind="verification-cell",
                        path_scope="cell",
                        owner=source_root.owner,
                    )
                else:
                    parsed_cells.append(
                        _load_cell(
                            source_root.owner,
                            source_root.manifest_path,
                            source_path.parent,
                            document,
                        )
                    )
            previous = expected_documents.get(source_path)
            if previous is not None and previous != document:
                raise ValueError("OA source snapshot document content drift")
            expected_documents[source_path] = document
        if tuple(parsed_cells) != source_root.cells:
            raise ValueError("OA source snapshot cell document drift")
        if not parsed_cells or any(
            not any(cell.directory.parent == cell_root for cell in parsed_cells)
            for cell_root in source_root.cell_roots
        ):
            raise ValueError("OA source snapshot source root declares no OA cells")
        if not isinstance(
            source_root.source_documents, _MAPPING_PROXY_TYPE
        ) or any(
            not is_frozen_toml_document(document)
            for document in source_root.source_documents.values()
        ):
            raise ValueError("OA source snapshot source-root documents are mutable")
        expected_cells.extend(source_root.cells)
    if tuple(expected_cells) != snapshot.cells:
        raise ValueError("OA source snapshot cell inventory drift")
    source_owners = tuple(item.owner for item in snapshot.source_roots)
    if len(set(source_owners)) != len(source_owners):
        raise ValueError("OA source snapshot contains duplicate source owners")
    cell_names = tuple(cell.cell for cell in snapshot.cells)
    if len(set(cell_names)) != len(cell_names):
        raise ValueError("OA source snapshot contains duplicate cell names")
    available_views = {
        OAViewReference(cell.cell, view.name)
        for cell in snapshot.cells
        for view in cell.views
    }
    unresolved = {
        dependency
        for cell in snapshot.cells
        for view in cell.views
        for dependency in view.dependencies
        if dependency not in available_views
    }
    if unresolved:
        raise ValueError("OA source snapshot has unresolved view dependencies")
    if snapshot.physical_verification is not None:
        policy = snapshot.physical_verification
        if (
            policy.path != policy.path.resolve()
            or not policy.path.is_relative_to(owner.root)
            or not policy.document
            or not is_frozen_toml_document(policy.document)
            or not isinstance(policy.drc_disabled_defines, _MAPPING_PROXY_TYPE)
            or parse_physical_verification_policy(
                policy.path,
                policy.document,
                owner=owner.name,
            )
            != policy
        ):
            raise ValueError("OA source snapshot physical policy drift")
        previous = expected_documents.get(policy.path)
        if previous is not None and previous != policy.document:
            raise ValueError("OA source snapshot document content drift")
        expected_documents[policy.path] = policy.document
    if dict(snapshot.source_documents) != expected_documents:
        raise ValueError("OA source snapshot document content drift")
    if not isinstance(snapshot.source_documents, _MAPPING_PROXY_TYPE) or any(
        not is_frozen_toml_document(document)
        for document in snapshot.source_documents.values()
    ):
        raise ValueError("OA source snapshot assembly documents are mutable")
    return snapshot
