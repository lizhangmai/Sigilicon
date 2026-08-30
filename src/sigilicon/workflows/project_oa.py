"""Project-bound entrypoint for canonical native OA workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.workflows.oa_check import check_oa_library
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    TestbenchRebuildStep,
    attest_oa_testbench,
    plan_oa_library_rebuild,
    rebuild_oa_library,
)


@dataclass(frozen=True)
class ProjectOaWorkflow:
    """Owner-bound native OA planning, checking, rebuilding, and execution."""

    project: Project
    owner_name: str
    _manifest: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        owner = self.project.owner(self.owner_name)
        manifest = self.project.oa_assembly_for(owner.root)
        if manifest is None:
            raise ValueError(f"project owner {owner.name!r} has no OA assembly")
        object.__setattr__(self, "owner_name", owner.name)
        object.__setattr__(self, "_manifest", manifest)

    @property
    def owner(self) -> RepositoryOwner:
        return self.project.owner(self.owner_name)

    @staticmethod
    def _testbench(
        plan: OALibraryRebuildPlan,
        testbench: str,
    ) -> TestbenchRebuildStep:
        matches = [step for step in plan.testbenches if step.cell == testbench]
        if len(matches) != 1:
            raise ValueError(f"unknown OA testbench in assembly: {testbench}")
        return matches[0]

    def plan(self) -> OALibraryRebuildPlan:
        return plan_oa_library_rebuild(
            self._manifest,
            project=self.project,
        )

    def check(
        self,
        *,
        client: Any,
        timeout: int = 300,
    ) -> dict[str, Any]:
        return check_oa_library(
            self._manifest,
            project=self.project,
            library=None,
            client=client,
            timeout=timeout,
        )

    def attest(
        self,
        *,
        testbench: str,
        client: Any,
        timeout: int = 300,
    ) -> dict[str, object]:
        plan = self.plan()
        return attest_oa_testbench(
            plan,
            self._testbench(plan, testbench),
            client,
            timeout=timeout,
        )

    def rebuild(
        self,
        *,
        client: Any,
        cell: str | None = None,
        testbench: str | None = None,
        timeout: int = 300,
        report: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        return rebuild_oa_library(
            self.plan(),
            client,
            cell=cell,
            testbench=testbench,
            timeout=timeout,
            report=report,
        )
