"""Project-bound entrypoint for canonical native OA workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sigilicon.domain.repository import Project
from sigilicon.workflows.oa_check import check_oa_library
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    TestbenchRebuildStep,
    attest_oa_testbench,
    plan_oa_library_rebuild,
    rebuild_oa_library,
)
from sigilicon.workflows.oa_simulation import (
    OAMaestroRunResult,
    run_oa_maestro_testbench,
)


@dataclass(frozen=True)
class ProjectOaWorkflow:
    """Own native OA planning, checking, rebuilding, and testbench execution."""

    project: Project

    @classmethod
    def from_file(cls, project_contract: Path | str) -> "ProjectOaWorkflow":
        return cls(Project.from_file(project_contract))

    def _manifest(self, value: Path | str) -> Path:
        candidate = Path(value)
        return (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.project.project_root / candidate).resolve()
        )

    @staticmethod
    def _testbench(
        plan: OALibraryRebuildPlan,
        testbench: str,
    ) -> TestbenchRebuildStep:
        matches = [step for step in plan.testbenches if step.cell == testbench]
        if len(matches) != 1:
            raise ValueError(f"unknown OA testbench in assembly: {testbench}")
        return matches[0]

    def plan(
        self,
        manifest: Path | str,
        *,
        library: str | None = None,
    ) -> OALibraryRebuildPlan:
        return plan_oa_library_rebuild(
            self._manifest(manifest),
            project=self.project,
            library=library,
        )

    def check(
        self,
        manifest: Path | str,
        *,
        library: str | None,
        client: Any,
        timeout: int = 300,
    ) -> dict[str, Any]:
        return check_oa_library(
            self._manifest(manifest),
            project=self.project,
            library=library,
            client=client,
            timeout=timeout,
        )

    def attest(
        self,
        manifest: Path | str,
        *,
        library: str | None,
        testbench: str,
        client: Any,
        timeout: int = 300,
    ) -> dict[str, object]:
        plan = self.plan(manifest, library=library)
        return attest_oa_testbench(
            plan,
            self._testbench(plan, testbench),
            client,
            timeout=timeout,
        )

    def simulate(
        self,
        manifest: Path | str,
        *,
        library: str | None,
        testbench: str,
        client: Any,
        timeout: int = 600,
    ) -> OAMaestroRunResult:
        plan = self.plan(manifest, library=library)
        return run_oa_maestro_testbench(
            plan,
            self._testbench(plan, testbench),
            client,
            timeout=timeout,
        )

    def rebuild(
        self,
        manifest: Path | str,
        *,
        library: str | None,
        client: Any,
        cell: str | None = None,
        testbench: str | None = None,
        timeout: int = 300,
        report: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        return rebuild_oa_library(
            self.plan(manifest, library=library),
            client,
            cell=cell,
            testbench=testbench,
            timeout=timeout,
            report=report,
        )
