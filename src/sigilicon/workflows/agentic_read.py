"""Client-neutral read and planning Interface for agent integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from sigilicon.domain.circuit_design import (
    design_artifact_from_json,
    design_candidate_from_json,
    design_decision_from_json,
)
from sigilicon.domain.repository import (
    Project,
    RepositoryOwner,
)
from sigilicon.workflows.project_runner import ProjectRunner
from sigilicon.workflows.design_artifacts import validate_candidate_records
from sigilicon.workflows.design_promotion import (
    compile_promotion_plan,
    promotion_request_from_json,
)
from sigilicon.workflows.source_control import inspect_source_state
from sigilicon.workflows.agentic_response import (
    READ_RESULT_KIND,
    public_value as _public_value,
    read_response,
    repository_identity,
)


@dataclass(frozen=True)
class AgenticReadInterface:
    """Resolve project-owned target operations through the planning Module."""

    project: Project
    project_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project, Project):
            raise TypeError("AgenticReadInterface requires a Project")
        object.__setattr__(
            self,
            "project_id",
            repository_identity(self.project.manifest_owner, self.project.context),
        )

    @classmethod
    def from_project(cls, project: Project) -> AgenticReadInterface:
        return cls(project)

    @property
    def project_resource_uri(self) -> str:
        return f"sigilicon://project/{self.project_id}"

    def owner_resource_uri(self, owner: str) -> str:
        selected = self._owner(owner)
        return f"sigilicon://owners/{selected.name}/targets"

    def inspect_project(self, *, owner: str | None) -> dict[str, Any]:
        """Project canonical owner/target projection with no site path disclosure."""

        selected = (
            self.project.owners
            if owner is None
            else (self._owner(owner),)
        )
        owners = [
            self._owner_payload(item)
            for item in selected
        ]
        source = inspect_source_state(self.project.project_root)
        resources = [self.project_resource_uri]
        resources.extend(self.owner_resource_uri(item.name) for item in selected)
        return self.response(
            operation="project.inspect",
            authority="source-contract",
            conclusion="valid",
            summary=(
                f"Validated {len(owners)} project owner"
                f"{'s' if len(owners) != 1 else ''}; runtime capability "
                "availability and product qualification were not evaluated."
            ),
            data={
                "project_source": {
                    "commit": source.commit,
                    "dirty": source.working_tree_dirty,
                    "repository_available": source.repository_available,
                },
                "owners": owners,
                "runtime_capabilities": {
                    "status": "not-evaluated",
                    "available": [],
                },
                "model_context": {
                    "source_text_trust": "untrusted",
                    "qualification_authority": "none",
                },
            },
            resources=resources,
            allowed_next_actions=["target.plan", "run.inspect"],
        )

    def plan_target(
        self,
        *,
        owner: str,
        target: str,
        operation: str,
    ) -> dict[str, Any]:
        """Compile one owner target operation without executing a backend."""

        resolved = ProjectRunner(
            self.project,
            owner,
        ).plan(target, operation)
        record = resolved.record
        return self.response(
            operation="target.plan",
            authority="plan",
            conclusion="planned",
            summary=(
                f"Resolved {resolved.target}/{resolved.operation} into "
                f"{resolved.node_count} typed "
                "nodes; no backend was executed."
            ),
            data={
                "plan_identity": resolved.plan_identity,
                "plan": _public_value(record),
            },
            resources=[
                self.project_resource_uri,
                self.owner_resource_uri(resolved.owner),
            ],
            allowed_next_actions=["project.inspect", "review-plan"],
        )

    def validate_candidate(
        self,
        *,
        owner: str,
        candidate_json: str,
        artifact_json: tuple[str, ...],
    ) -> dict[str, Any]:
        """Validate an immutable Candidate chain inside one project owner."""

        selected_owner = self._owner(owner)
        validation = validate_candidate_records(
            candidate_json,
            artifact_json,
            expected_owner=selected_owner.name,
        )
        return self.response(
            operation="candidate.validate",
            authority="plan",
            conclusion="valid",
            summary=(
                "Validated the Candidate and "
                f"{len(validation.resolved_artifacts)} exact referenced artifacts; "
                "no engineering pass or source promotion was performed."
            ),
            data={
                "candidate_identity": validation.candidate_identity,
                "resolved_artifacts": list(validation.resolved_artifacts),
            },
            resources=[
                self.project_resource_uri,
                self.owner_resource_uri(selected_owner.name),
            ],
            allowed_next_actions=["project.inspect", "review-candidate"],
        )

    def plan_candidate_promotion(
        self,
        *,
        owner: str,
        candidate_json: str,
        artifact_json: tuple[str, ...],
        decision_json: str,
        request_json: str,
    ) -> dict[str, Any]:
        """Compile a human-review plan while exposing no source-write operation."""

        selected_owner = self._owner(owner)
        candidate = design_candidate_from_json(candidate_json)
        decision = design_decision_from_json(decision_json)
        artifacts = tuple(design_artifact_from_json(text) for text in artifact_json)
        request = promotion_request_from_json(request_json)
        if candidate.metadata.owner != selected_owner.name:
            raise ValueError("Promotion Candidate owner drift")
        plan = compile_promotion_plan(
            candidate=candidate,
            artifacts=artifacts,
            decision=decision,
            request=request,
        )
        return self.response(
            operation="candidate.promotion_plan",
            authority="plan",
            conclusion="ready_for_human_review",
            summary=(
                "Compiled an immutable Promotion Plan; human approval and ordinary "
                "Git review remain required, and no canonical source was written."
            ),
            data={
                "promotion_plan_identity": plan.identity,
                "promotion_plan": json.loads(plan.canonical_json()),
                "human_approval_required": True,
                "writes_canonical_source": False,
            },
            resources=[
                self.project_resource_uri,
                self.owner_resource_uri(selected_owner.name),
            ],
            allowed_next_actions=["review-promotion-plan", "request-human-approval"],
        )

    def _owner(self, name: str) -> RepositoryOwner:
        return self.project.owner(name)

    def _targets(self, owner: RepositoryOwner) -> list[dict[str, Any]]:
        if owner.component.target_catalog is None:
            return []
        descriptions = ProjectRunner(self.project, owner.name).targets()
        return [
            {
                "name": description["name"],
                "description": description["description"],
                "operations": list(description["operations"]),
            }
            for description in descriptions
        ]

    def _owner_payload(
        self,
        owner: RepositoryOwner,
    ) -> dict[str, Any]:
        root = self.project.project_root
        component = owner.component
        return {
            "name": owner.name,
            "component": component.name,
            "kind": component.kind,
            "component_contract": component.path.relative_to(root).as_posix(),
            "public_interface": (
                None
                if component.public_interface is None
                else component.public_interface.as_posix()
            ),
            "filesets": [
                {"name": name, "member_count": len(members)}
                for name, members in sorted(component.filesets.items())
            ],
            "targets": self._targets(owner),
        }

    def response(
        self,
        *,
        operation: str,
        authority: str,
        conclusion: str,
        summary: str,
        data: dict[str, Any],
        resources: list[str],
        allowed_next_actions: list[str],
    ) -> dict[str, Any]:
        return read_response(
            project_id=self.project_id,
            operation=operation,
            authority=authority,
            conclusion=conclusion,
            summary=summary,
            data=data,
            resources=resources,
            allowed_next_actions=allowed_next_actions,
        )


__all__ = [
    "AgenticReadInterface",
    "READ_RESULT_KIND",
]
