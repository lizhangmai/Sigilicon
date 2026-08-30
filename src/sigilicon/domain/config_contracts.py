"""Shared metadata checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import os
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from sigilicon.artifacts import read_nofollow_text

if TYPE_CHECKING:
    from sigilicon.domain.repository import OwnerCatalogSnapshot, Project


CONFIG_SCHEMA = 1
PATH_SCOPES = frozenset(
    {"repository", "owner", "cell", "verification", "platform", "variant"}
)
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def freeze_toml_document(value: Any) -> Any:
    """Recursively freeze one parsed TOML value for operation snapshots."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_toml_document(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_toml_document(item) for item in value)
    return value


def is_frozen_toml_document(value: object) -> bool:
    """Return whether one parsed TOML value is recursively immutable."""

    if isinstance(value, _MAPPING_PROXY_TYPE):
        return all(
            isinstance(key, str) and is_frozen_toml_document(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return all(is_frozen_toml_document(item) for item in value)
    return isinstance(value, (str, int, float, bool, datetime, date, time))


def thaw_toml_document(value: Any) -> Any:
    """Copy a frozen TOML value back to the parser's dict/list shape."""

    if isinstance(value, Mapping):
        return {key: thaw_toml_document(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_toml_document(item) for item in value]
    return value


@dataclass(frozen=True)
class ConfigHeader:
    schema: int
    contract_kind: str
    path_scope: str
    owner: str


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_config_header(
    raw: Mapping[str, Any],
    path: Path,
    *,
    contract_kind: str | tuple[str, ...],
    path_scope: str | tuple[str, ...],
    owner: str | None = None,
    schema: int = CONFIG_SCHEMA,
) -> ConfigHeader:
    """Validate the common header for one domain-specific TOML document."""

    actual_schema = raw.get("schema")
    if isinstance(actual_schema, bool) or actual_schema != schema:
        raise ValueError(f"{path}: schema must be {schema}")
    actual_kind = _text(raw.get("contract_kind"), f"{path}: contract_kind")
    allowed_kinds = (contract_kind,) if isinstance(contract_kind, str) else contract_kind
    if actual_kind not in allowed_kinds:
        raise ValueError(
            f"{path}: contract_kind must be one of {sorted(allowed_kinds)}"
        )
    actual_scope = _text(raw.get("path_scope"), f"{path}: path_scope")
    allowed_scopes = (path_scope,) if isinstance(path_scope, str) else path_scope
    if actual_scope not in allowed_scopes or actual_scope not in PATH_SCOPES:
        raise ValueError(
            f"{path}: path_scope must be one of {sorted(set(allowed_scopes) & PATH_SCOPES)}"
        )
    actual_owner = _text(raw.get("owner"), f"{path}: owner")
    if owner is not None and actual_owner != owner:
        raise ValueError(f"{path}: owner must be {owner!r}, got {actual_owner!r}")
    return ConfigHeader(
        schema=schema,
        contract_kind=actual_kind,
        path_scope=actual_scope,
        owner=actual_owner,
    )


def read_toml(path: Path) -> dict[str, Any]:
    """Read one TOML document and require a table root."""

    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return value


_REPOSITORY_SOURCE_INVENTORY_AUTHORITY = object()


def _repository_configuration_roots(project: Project) -> frozenset[Path]:
    """Return the catalog-selected roots scanned by repository checks."""

    return frozenset(
        {
            *(owner.root for owner in project.owners),
            *(path.parent for _, path in project.catalog_paths),
        }
    )


def _repository_configuration_paths(directory: Path) -> tuple[Path, ...]:
    """Enumerate regular TOML sources while rejecting symlinked subtrees."""

    if directory != directory.resolve() or not directory.is_dir():
        raise ValueError(
            f"configuration owner root is missing or unsafe: {directory}"
        )
    pending = [directory]
    result: list[Path] = []
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanned:
                entries = sorted(scanned, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(
                f"cannot inspect configuration owner root {current}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    if path.suffix == ".toml" or entry.is_dir():
                        raise ValueError(
                            f"configuration source path is a symlink: {path}"
                        )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if path.suffix == ".toml":
                        result.append(path)
                elif path.suffix == ".toml":
                    raise ValueError(
                        f"configuration source must be a regular file: {path}"
                    )
            except OSError as exc:
                raise ValueError(
                    f"cannot inspect configuration source {path}: {exc}"
                ) from exc
    return tuple(result)


def _read_frozen_toml(path: Path) -> Mapping[str, Any]:
    """Capture one exact regular TOML source without following links."""

    try:
        value = tomllib.loads(read_nofollow_text(path))
    except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    return freeze_toml_document(value)


class RepositorySourceInventory:
    """Immutable source closure trusted by one repository scan operation.

    Construction captures every TOML below the catalog-selected scan roots.
    Domain loaders may then verify their richer snapshots against this closed
    inventory, but they cannot add sources discovered later in the operation.
    """

    __slots__ = ("_documents", "_project")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        documents: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        if _authority is not _REPOSITORY_SOURCE_INVENTORY_AUTHORITY:
            raise ValueError("repository source inventory must use its factory")
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_documents", MappingProxyType(dict(documents)))

    @classmethod
    def for_project(
        cls,
        project: Project,
    ) -> RepositorySourceInventory:
        """Capture the complete configuration inventory for one operation."""

        expected_documents: dict[Path, Mapping[str, Any]] = {}

        def seed(label: str, path: Path, document: Mapping[str, Any]) -> None:
            resolved = path.resolve()
            if (
                path != resolved
                or not resolved.is_relative_to(project.project_root)
                or not is_frozen_toml_document(document)
            ):
                raise ValueError(f"{label} source identity drift: {path}")
            previous = expected_documents.get(resolved)
            if previous is not None and previous != document:
                raise ValueError(f"{label} disagrees with another source: {path}")
            expected_documents[resolved] = document

        manifest_document = project.manifest_source_document()
        if manifest_document:
            seed(
                "project manifest snapshot",
                project.manifest_path,
                manifest_document,
            )
        for owner in project.owners:
            seed(
                "owner component snapshot",
                owner.component.path,
                owner.component.document,
            )
        ip_catalog = project.ip_catalog_snapshot()
        seed(
            "IP catalog snapshot",
            ip_catalog.path,
            ip_catalog.document,
        )
        paths = {
            project.manifest_path,
            *(path for _, path in project.catalog_paths),
        }
        for directory in _repository_configuration_roots(project):
            paths.update(_repository_configuration_paths(directory))
        documents: dict[Path, Mapping[str, Any]] = {}
        for path in sorted(paths):
            resolved = path.resolve()
            if path != resolved or not resolved.is_relative_to(project.project_root):
                raise ValueError(f"configuration source is unsafe: {path}")
            documents[resolved] = _read_frozen_toml(resolved)
        inventory = cls(
            _authority=_REPOSITORY_SOURCE_INVENTORY_AUTHORITY,
            project=project,
            documents=documents,
        )
        inventory.verify("project source snapshot", expected_documents)
        return inventory

    @property
    def project(self) -> Project:
        return self._project

    @property
    def documents(self) -> Mapping[Path, Mapping[str, Any]]:
        return self._documents

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("repository source inventory is immutable")

    def require_project(self, project: Project) -> None:
        """Require the exact operation that captured this inventory."""

        if project is not self.project:
            raise ValueError("repository source inventory belongs to another operation")

    def verify(
        self,
        label: str,
        source_documents: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        """Require domain snapshots to match the captured source inventory."""

        if not isinstance(label, str) or not label:
            raise ValueError("repository source inventory label must be non-empty")
        if not isinstance(source_documents, Mapping):
            raise TypeError("repository source documents must be a mapping")
        for path, document in source_documents.items():
            if (
                not isinstance(path, Path)
                or path != path.resolve()
                or not path.is_relative_to(self.project.project_root)
                or path.suffix != ".toml"
            ):
                raise ValueError(f"{label} source path identity drift: {path}")
            if not is_frozen_toml_document(document):
                raise ValueError(f"{label} source document must be frozen: {path}")
            previous = self.documents.get(path)
            if previous is None:
                raise ValueError(
                    f"{label} source is outside the captured inventory: {path}"
                )
            if previous != document:
                raise ValueError(f"{label} disagrees with another source: {path}")

    def resolve(self, path: Path) -> Mapping[str, Any]:
        """Return one captured document or reject an inventory miss."""

        if not isinstance(path, Path) or path != path.resolve():
            raise ValueError("repository source inventory lookup must be resolved")
        try:
            return self.documents[path]
        except KeyError as exc:
            raise ValueError(
                f"configuration source is outside the captured inventory: {path}"
            ) from exc


def inspect_project_configuration_sources(
    context: Project,
    *,
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    sources: RepositorySourceInventory,
) -> dict[str, Any]:
    """Inspect configuration envelopes from one closed source inventory."""

    sources.require_project(context)
    sources.verify(
        "owner flow catalog snapshot",
        {snapshot.path: snapshot.document for snapshot in catalog_inventory},
    )
    root = context.project_root.resolve()
    project_contract = context.manifest_path
    repository_owner = context.manifest_owner
    design_catalogs = context.flow_catalog_snapshots(
        "design_targets",
        inventory=catalog_inventory,
    )
    layout_catalogs = context.flow_catalog_snapshots(
        "layout_targets",
        inventory=catalog_inventory,
    )
    exact_paths = {
        project_contract,
        *(path for _, path in context.catalog_paths),
        *(snapshot.path for snapshot in design_catalogs),
        *(snapshot.path for snapshot in layout_catalogs),
    }
    repository_owner_roots = {owner.root for owner in context.owners}
    scan_roots = _repository_configuration_roots(context)

    resolved_owner_roots = {
        owner.root: owner.name for owner in context.owners
    }
    platform_catalog_path = context.catalog("platform")
    platform_catalog_document = sources.resolve(platform_catalog_path)
    platforms = platform_catalog_document.get("platforms")
    if not isinstance(platforms, Mapping):
        raise ValueError("platform catalog platforms must be a table")
    for key, value in platforms.items():
        if not isinstance(key, str) or not key:
            raise ValueError("platform catalog keys must be non-empty strings")
        if not isinstance(value, str) or not value:
            raise ValueError(f"platforms.{key} must be a non-empty relative path")
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or "\\" in value
            or relative.as_posix() != value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError(
                f"platforms.{key} must be a canonical relative path"
            )
        manifest = platform_catalog_path.parent.joinpath(*relative.parts).resolve()
        if not manifest.is_relative_to(root):
            raise ValueError(f"platforms.{key} manifest is missing or unsafe")
        document = sources.resolve(manifest)
        owner = _text(document.get("owner"), f"{manifest}: owner")
        platform_owner_root = manifest.parent
        previous = resolved_owner_roots.get(platform_owner_root)
        if previous is not None and previous != owner:
            raise ValueError(
                "platform and component catalogs disagree on an owner root"
            )
        resolved_owner_roots[platform_owner_root] = owner

    for path in (*exact_paths, *scan_roots, *resolved_owner_roots):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
    documents = set(sources.documents)
    missing_exact = exact_paths - documents
    if missing_exact:
        raise ValueError(
            "configuration source inventory omitted selected contracts: "
            f"{sorted(missing_exact)}"
        )

    contract_kinds: set[str] = set()
    owners: set[str] = set()
    native_documents = 0
    envelope_fields = frozenset({"contract_kind", "path_scope", "owner"})
    repository_sources = {project_contract, *(path for _, path in context.catalog_paths)}
    platform_root = context.catalog("platform").parent
    for path in sorted(documents):
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"configuration source escapes the project root: {path}")
        raw = sources.resolve(resolved)
        present = envelope_fields & raw.keys()
        if not present:
            native_documents += 1
            continue
        if present != envelope_fields:
            missing = sorted(envelope_fields - present)
            raise ValueError(f"{resolved}: incomplete configuration header: {missing}")
        kind = _text(raw.get("contract_kind"), f"{resolved}: contract_kind")
        if resolved in repository_sources:
            allowed_scopes: str | tuple[str, ...] = "repository"
            expected_owner = repository_owner
        else:
            matches = [
                (owner_root, owner)
                for owner_root, owner in resolved_owner_roots.items()
                if resolved.is_relative_to(owner_root)
            ]
            if not matches:
                raise ValueError(f"{resolved}: configuration has no cataloged owner")
            owner_root, expected_owner = max(
                matches,
                key=lambda item: len(item[0].parts),
            )
            if owner_root in repository_owner_roots:
                allowed_scopes = ("owner", "cell", "verification", "variant")
            elif owner_root.is_relative_to(platform_root):
                allowed_scopes = "platform"
            else:
                raise ValueError(
                    f"{owner_root}: configuration owner root has no domain catalog"
                )
        header = require_config_header(
            raw,
            resolved,
            contract_kind=kind,
            path_scope=allowed_scopes,
            owner=expected_owner,
        )
        if header.contract_kind == "verification-cell":
            from sigilicon.domain.verification_cell import parse_verification_cell

            spec = parse_verification_cell(
                resolved,
                raw,
                project=context,
                contract_documents=sources.documents,
            )
            for source_path, document in spec.source_documents.items():
                sources.verify(
                    "verification cell snapshot",
                    {source_path: document},
                )
        contract_kinds.add(header.contract_kind)
        owners.add(header.owner)

    return {
        "passed": True,
        "roots": sorted(
            path.relative_to(root).as_posix() for path in scan_roots
        ),
        "documents": len(documents),
        "contracts": len(documents) - native_documents,
        "native_documents": native_documents,
        "contract_kinds": sorted(contract_kinds),
        "owners": sorted(owners),
    }
