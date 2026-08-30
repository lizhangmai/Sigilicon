"""Shared metadata checks for repository-owned TOML contracts.

The repository intentionally keeps several domain-specific TOML schemas.  This
module does not flatten those schemas; it validates the small common envelope
that makes ownership, scope, and migration boundaries explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

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


_REPOSITORY_SOURCE_LEDGER_AUTHORITY = object()


class RepositorySourceLedger:
    """Immutable source closure trusted by one repository scan operation.

    The ledger knows no IP, OA, layout, or platform schema. The workflow that
    owns those snapshots validates them first and contributes only their frozen
    source documents. A partial ledger is valid; scanner misses continue to use
    the existing filesystem fallback.
    """

    __slots__ = ("_documents", "_project")

    def __init__(
        self,
        *,
        _authority: object,
        project: Project,
        documents: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        if _authority is not _REPOSITORY_SOURCE_LEDGER_AUTHORITY:
            raise ValueError("repository source ledger must use its factory")
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_documents", MappingProxyType(dict(documents)))

    @classmethod
    def for_project(
        cls,
        project: Project,
        *,
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...] = (),
    ) -> RepositorySourceLedger:
        """Seed one ledger with already-loaded project and owner catalogs."""

        ledger = cls(
            _authority=_REPOSITORY_SOURCE_LEDGER_AUTHORITY,
            project=project,
            documents={},
        )
        manifest_document = project.manifest_source_document()
        if manifest_document:
            ledger = ledger.merge(
                "project manifest snapshot",
                {project.manifest_path: manifest_document},
            )
        for owner in project.owners:
            ledger = ledger.merge(
                "owner component snapshot",
                {owner.component.path: owner.component.document},
            )
        ip_catalog = project.ip_catalog_snapshot()
        ledger = ledger.merge(
            "IP catalog snapshot",
            {ip_catalog.path: ip_catalog.document},
        )
        for snapshot in catalog_inventory:
            ledger = ledger.merge(
                "owner flow catalog snapshot",
                {snapshot.path: snapshot.document},
            )
        return ledger

    @property
    def project(self) -> Project:
        return self._project

    @property
    def documents(self) -> Mapping[Path, Mapping[str, Any]]:
        return self._documents

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("repository source ledger is immutable")

    def require_project(self, project: Project) -> None:
        """Require the exact operation that created this ledger."""

        if project is not self.project:
            raise ValueError("repository source ledger belongs to another operation")

    def merge(
        self,
        label: str,
        source_documents: Mapping[Path, Mapping[str, Any]],
    ) -> RepositorySourceLedger:
        """Return a ledger extended by one validated domain source set."""

        if not isinstance(label, str) or not label:
            raise ValueError("repository source ledger label must be non-empty")
        if not isinstance(source_documents, Mapping):
            raise TypeError("repository source documents must be a mapping")
        root = self.project.project_root
        merged = dict(self.documents)
        for path, document in source_documents.items():
            if (
                not isinstance(path, Path)
                or path != path.resolve()
                or not path.is_relative_to(root)
                or not path.is_file()
            ):
                raise ValueError(f"{label} source path identity drift: {path}")
            if not is_frozen_toml_document(document):
                raise ValueError(f"{label} source document must be frozen: {path}")
            previous = merged.get(path)
            if previous is not None and previous != document:
                raise ValueError(f"{label} disagrees with another source: {path}")
            merged[path] = document
        return RepositorySourceLedger(
            _authority=_REPOSITORY_SOURCE_LEDGER_AUTHORITY,
            project=self.project,
            documents=merged,
        )

    def resolve(self, path: Path) -> Mapping[str, Any] | None:
        """Return one exact seeded document without filesystem fallback."""

        if not isinstance(path, Path) or path != path.resolve():
            raise ValueError("repository source ledger lookup must be resolved")
        return self.documents.get(path)


def inspect_project_configuration_sources(
    context: Project,
    *,
    catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    sources: RepositorySourceLedger,
) -> dict[str, Any]:
    """Scan configuration envelopes from one operation-owned source ledger."""

    sources.require_project(context)
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
    scan_roots = set(repository_owner_roots)
    scan_roots.update(
        path.parent
        for _, path in context.catalog_paths
    )

    operation_documents = dict(sources.documents)
    resolved_owner_roots = {
        owner.root: owner.name for owner in context.owners
    }
    platform_catalog_path = context.catalog("platform")
    platform_catalog_document = operation_documents.get(platform_catalog_path)
    if platform_catalog_document is None:
        platform_catalog_document = read_toml(platform_catalog_path)
        operation_documents[platform_catalog_path] = platform_catalog_document
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
        if not manifest.is_relative_to(root) or not manifest.is_file():
            raise ValueError(f"platforms.{key} manifest is missing or unsafe")
        document = operation_documents.get(manifest)
        if document is None:
            document = read_toml(manifest)
            operation_documents[manifest] = document
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
    for path in exact_paths:
        if not path.is_file():
            raise ValueError(f"configuration source is missing: {path}")
    for directory in scan_roots:
        if not directory.is_dir():
            raise ValueError(f"configuration owner root is missing: {directory}")

    documents = set(exact_paths)
    for directory in scan_roots:
        documents.update(path for path in directory.rglob("*.toml") if path.is_file())

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
        if raw is None:
            raw = operation_documents.get(resolved)
        if raw is None:
            raw = read_toml(resolved)
            operation_documents[resolved] = raw
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
                contract_documents=operation_documents,
            )
            for source_path, document in spec.source_documents.items():
                operation_documents.setdefault(source_path, document)
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
