"""Read persisted Run records without consulting mutable source catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sigilicon.domain.repository import Project
from sigilicon.execution import RunStore
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.identifiers import bounded_identity
from sigilicon.paths import ProjectContext
from sigilicon.workflows.agentic_response import (
    public_value,
    read_response,
    repository_identity,
)
from sigilicon.workflows.agentic_runs import AgenticRunStore, RUNNING_STATUSES


@dataclass(frozen=True)
class RunReadInterface:
    """Inspect exact immutable and managed Runs using only bound runtime paths."""

    context: ProjectContext
    project_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProjectContext):
            raise TypeError("RunReadInterface requires a ProjectContext")
        bounded_identity(self.project_id, "agentic project")

    @classmethod
    def from_project(cls, project: Project) -> RunReadInterface:
        return cls(
            project.context,
            repository_identity(project.manifest_owner, project.context),
        )

    def resource_uri(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> str:
        owner_name = owner_identity(owner, "project owner")
        target_name = identifier(target, "project target")
        operation_name = identifier(operation, "target operation")
        identity = run_identity(run_id)
        return (
            f"sigilicon://owners/{owner_name}/targets/{target_name}/"
            f"operations/{operation_name}/runs/{identity}/manifest"
        )

    def inspect(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
        run_id: str,
    ) -> dict[str, Any]:
        selected_owner = owner_identity(owner, "project owner")
        target_name = identifier(target, "project target")
        operation_name = identifier(operation, "target operation")
        identity = run_identity(run_id)
        runs = RunStore(self.context)
        managed = AgenticRunStore(self.context, self.project_id)
        managed_paths = managed.paths(
            owner=selected_owner,
            target=target_name,
            operation=operation_name,
            run_id=identity,
        )
        state_path = managed_paths.role("control") / "state.json"
        if state_path.exists():
            state = managed.read_state(managed_paths)
            if (
                state["owner"] != selected_owner
                or state["target"] != target_name
                or state["operation"] != operation_name
                or state["run_id"] != identity
            ):
                raise ValueError("managed target-operation run identity drift")
            result = runs.read_if_present(
                owner=selected_owner,
                target=target_name,
                operation=operation_name,
                run_id=identity,
            )
            resource = self.resource_uri(
                owner=selected_owner,
                target=target_name,
                operation=operation_name,
                run_id=identity,
            )
            return read_response(
                project_id=self.project_id,
                operation="run.inspect",
                authority=(
                    "managed-run-state"
                    if result is None
                    else "recorded-target-operation-result"
                ),
                conclusion=(
                    "in-progress"
                    if state["status"] in RUNNING_STATUSES
                    else state["status"]
                ),
                summary=(
                    f"Managed target operation run {identity} is {state['status']!r}; "
                    "no additional qualification claim was made."
                ),
                data={
                    "management": state,
                    "result": None if result is None else public_value(result),
                    "model_context": {
                        "record_text_trust": "untrusted",
                        "qualification_authority": "record-only",
                    },
                },
                resources=[resource],
                allowed_next_actions=(
                    ["run.inspect", "run.cancel"]
                    if state["status"] in RUNNING_STATUSES
                    else ["project.inspect", "run.inspect"]
                ),
            )
        result = runs.read(
            owner=selected_owner,
            target=target_name,
            operation=operation_name,
            run_id=identity,
        )
        resource = self.resource_uri(
            owner=selected_owner,
            target=target_name,
            operation=operation_name,
            run_id=identity,
        )
        return read_response(
            project_id=self.project_id,
            operation="run.inspect",
            authority="recorded-target-operation-result",
            conclusion="recorded",
            summary=(
                "Read the identity-matched target-operation result with status "
                f"{result.get('status', 'unknown')!r}; no qualification claim was added."
            ),
            data={
                "result": public_value(result),
                "model_context": {
                    "record_text_trust": "untrusted",
                    "qualification_authority": "record-only",
                },
            },
            resources=[resource],
            allowed_next_actions=["project.inspect", "run.inspect"],
        )

    def inspect_managed(self, run_id: str) -> dict[str, Any]:
        """Locate one managed Run identity, then inspect its recorded selector."""

        located = AgenticRunStore(self.context, self.project_id).locate(run_id)
        state = located.state
        return self.inspect(
            owner=state["owner"],
            target=state["target"],
            operation=state["operation"],
            run_id=state["run_id"],
        )


__all__ = ["RunReadInterface"]
