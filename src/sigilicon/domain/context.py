"""Pure project composition identity retained by domain snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sigilicon.project import Project


@dataclass(frozen=True)
class RepositoryIdentity:
    """Stable project facts at either repository or owner scope."""

    project_root: Path
    workspace_root: Path
    owner: str | None
    identity: str

    @classmethod
    def for_repository(cls, project: Project) -> "RepositoryIdentity":
        return cls(
            project_root=project.project_root,
            workspace_root=project.workspace_root,
            owner=None,
            identity=project.identity,
        )

    @classmethod
    def for_owner(cls, project: Project, owner: str) -> "RepositoryIdentity":
        selected = project.owner(owner)
        return cls(
            project_root=project.project_root,
            workspace_root=project.workspace_root,
            owner=selected.name,
            identity=project.operation_identity(selected.name),
        )

    @classmethod
    def for_path(cls, project: Project, path: Path | str) -> "RepositoryIdentity":
        owner = project.owner_for(path)
        return (
            cls.for_repository(project)
            if owner is None
            else cls.for_owner(project, owner.name)
        )

    def validate(self, project: Project) -> None:
        current = (
            type(self).for_repository(project)
            if self.owner is None
            else type(self).for_owner(project, self.owner)
        )
        if current != self:
            raise ValueError("domain snapshot belongs to another project composition")


__all__ = ["RepositoryIdentity"]
