"""Pure repository identity shared by domain snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sigilicon.project import Project


@dataclass(frozen=True)
class RepositoryIdentity:
    """Stable repository facts retained by a pure domain snapshot."""

    project_root: Path
    workspace_root: Path
    identity: str

    @classmethod
    def capture(cls, project: Project) -> "RepositoryIdentity":
        return cls(
            project_root=project.project_root,
            workspace_root=project.workspace_root,
            identity=project.identity,
        )

    def validate(self, project: Project) -> None:
        current = type(self).capture(project)
        if current != self:
            raise ValueError("domain snapshot belongs to another project composition")


__all__ = ["RepositoryIdentity"]
