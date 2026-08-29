"""Project-bound access to design and layout target catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sigilicon.domain.repository import Project
from sigilicon.workflows.design_targets import (
    DesignTargetCatalog,
    load_design_target_catalog,
)
from sigilicon.workflows.layout_targets import (
    LayoutTargetCatalog,
    load_layout_target_catalog,
)
from sigilicon.workflows.project_layout import ProjectLayoutWorkflow


@dataclass(frozen=True)
class ProjectTargets:
    """Expose every target catalog through one canonical Project."""

    project: Project

    @classmethod
    def from_file(cls, project_contract: Path | str) -> "ProjectTargets":
        return cls(Project.from_file(project_contract))

    def design(self) -> DesignTargetCatalog:
        return load_design_target_catalog(project=self.project)

    def layout(self) -> LayoutTargetCatalog:
        return load_layout_target_catalog(project=self.project)

    def layout_workflow(self) -> ProjectLayoutWorkflow:
        return ProjectLayoutWorkflow(self.project)
