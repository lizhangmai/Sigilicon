"""Project-bound entrypoint for canonical layout workflows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.domain.repository import Project
from sigilicon.layout.spec import LayoutSpec, load_layout_spec
from sigilicon.workflows.layout_generation import (
    LayoutGenerationResult,
    LayoutPlanningResult,
    generate_layout,
)
from sigilicon.workflows.layout_verification import (
    LayoutVerificationResult,
    verify_layout,
)


@dataclass(frozen=True)
class ProjectLayoutWorkflow:
    """Plan, generate, and verify layouts through one canonical Project."""

    project: Project

    @classmethod
    def from_file(cls, project_contract: Path | str) -> "ProjectLayoutWorkflow":
        return cls(Project.from_file(project_contract))

    def plan(self, spec_path: Path) -> LayoutPlanningResult:
        spec = load_layout_spec(spec_path, project=self.project)
        return LayoutPlanningResult(spec)

    def generate(
        self,
        spec_path: Path,
        client: Any,
        *,
        timeout: int = 120,
    ) -> tuple[LayoutSpec, LayoutGenerationResult]:
        spec = load_layout_spec(spec_path, project=self.project)
        return spec, generate_layout(spec, client, timeout=timeout)

    def verify(
        self,
        spec_path: Path,
        client: Any,
        *,
        checks: Sequence[str],
        xstream_timeout: int = 120,
        calibre_timeout: int = 600,
    ) -> tuple[tuple[LayoutSpec, LayoutVerificationResult], ...]:
        spec = load_layout_spec(spec_path, project=self.project)
        return tuple(
            (
                spec,
                verify_layout(
                    spec,
                    client,
                    check=check,
                    xstream_timeout=xstream_timeout,
                    calibre_timeout=calibre_timeout,
                ),
            )
            for check in checks
        )
