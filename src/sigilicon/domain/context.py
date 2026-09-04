"""Pure repository identity shared by domain snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol


class RepositoryContext(Protocol):
    """Project capabilities needed while loading domain contracts."""

    project_root: Path
    workspace_root: Path
    artifact_root: Path
    identity: str
    manifest_path: Path
    manifest_owner: str
    owners: tuple[Any, ...]
    catalog_paths: tuple[tuple[str, Path], ...]
    configuration_roots: tuple[Path, ...]
    component_inventory: Mapping[Path, Any]

    def owner(self, name: str) -> Any: ...

    def owner_for(self, path: Path | str) -> Any | None: ...

    def require_owner(self, path: Path | str) -> Any: ...

    def catalog(self, name: str) -> Path: ...

    def find_catalog(self, name: str) -> Path | None: ...

    def manifest_source_document(self) -> Mapping[str, Any]: ...

    def resolve_owner_file(
        self,
        owner: str,
        relative: PurePosixPath | str,
        label: str,
    ) -> tuple[Path, PurePosixPath]: ...

    def resources(self) -> Any: ...


@dataclass(frozen=True)
class RepositoryIdentity:
    """Stable repository facts retained by a pure domain snapshot."""

    project_root: Path
    workspace_root: Path
    identity: str

    @classmethod
    def capture(cls, context: RepositoryContext) -> "RepositoryIdentity":
        return cls(
            project_root=context.project_root,
            workspace_root=context.workspace_root,
            identity=context.identity,
        )

    def validate(self, context: RepositoryContext) -> None:
        current = type(self).capture(context)
        if current != self:
            raise ValueError("domain snapshot belongs to another project composition")


__all__ = ["RepositoryContext", "RepositoryIdentity"]
