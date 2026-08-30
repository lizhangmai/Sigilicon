"""Client-neutral read and planning Interface for agent integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from sigilicon.domain.circuit_design import (
    design_artifact_from_json,
    design_candidate_from_json,
    design_decision_from_json,
)
from sigilicon.domain.repository import (
    OwnerCatalogSnapshot,
    Project,
    RepositoryOwner,
)
from sigilicon.flow import (
    parse_flow_catalog,
    resolve_catalog_selection,
)
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.workflows.project_flow import (
    ProjectFlow,
)
from sigilicon.workflows.design_artifacts import validate_candidate_records
from sigilicon.workflows.design_campaign import (
    resolve_project_design_campaign,
)
from sigilicon.workflows.design_targets import load_design_target_catalog
from sigilicon.workflows.design_promotion import (
    compile_promotion_plan,
    promotion_request_from_json,
)
from sigilicon.workflows.layout_targets import load_layout_target_catalog
from sigilicon.workflows.source_control import inspect_source_state
from sigilicon.workflows.agentic_runs import AgenticRunStore, RUNNING_STATUSES


READ_RESULT_KIND = "agentic-read-result"
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s\"'<>]+)")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>]+")
_SENSITIVE_FIELDS = frozenset(
    {"command", "commands", "env", "environment", "executable", "raw_log"}
)


def _repository_identity(project: Project) -> str:
    return f"{project.manifest_owner}.{project.project_root.name}"


def _public_value(value: Any, *, field: str | None = None) -> Any:
    """Bound untrusted records and remove site-private execution material."""

    if field in _SENSITIVE_FIELDS and not (
        field == "executable" and isinstance(value, bool)
    ):
        return "<redacted-private-execution-material>"
    if isinstance(value, dict):
        return {
            str(key): _public_value(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        if len(value) > 4000:
            return "<redacted-oversized-text>"
        return _WINDOWS_PATH.sub(
            "<redacted-site-path>",
            _ABSOLUTE_PATH.sub("<redacted-site-path>", value),
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("record contains a non-portable public value")


@dataclass(frozen=True)
class AgenticReadInterface:
    """Resolve only cataloged project identities through existing domain Modules."""

    project: Project
    project_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _repository_identity(self.project))

    @property
    def project_resource_uri(self) -> str:
        return f"sigilicon://project/{self.project_id}"

    def owner_resource_uri(self, owner: str) -> str:
        selected = self._owner(owner)
        return f"sigilicon://owners/{selected.name}/catalog"

    def run_resource_uri(
        self,
        *,
        owner: str,
        flow: str,
        target: str,
        run_id: str,
    ) -> str:
        owner_name = owner_identity(owner, "project owner")
        flow_name = identifier(flow, "Flow identity")
        target_name = identifier(target, "Flow target")
        identity = run_identity(run_id)
        return (
            f"sigilicon://runs/{owner_name}/{flow_name}/"
            f"{target_name}/{identity}/manifest"
        )

    def inspect_project(self, *, owner: str | None) -> dict[str, Any]:
        """Project canonical owner/target projection with no site path disclosure."""

        selected = (
            self.project.owners
            if owner is None
            else (self._owner(owner),)
        )
        catalog_inventory = self.project.flow_catalog_inventory()
        design_targets = self._design_targets(catalog_inventory)
        layout_targets = self._layout_targets(catalog_inventory)
        owners = [
            self._owner_payload(
                item,
                design_targets,
                layout_targets,
                catalog_inventory,
            )
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
                f"Validated {len(owners)} cataloged owner"
                f"{'s' if len(owners) != 1 else ''}; runtime capability "
                "availability and product qualification were not evaluated."
            ),
            data={
                "project_source": {
                    "commit": source.commit,
                    "dirty": source.working_tree_dirty,
                    "repository_available": source.repository_available,
                },
                "catalogs": [name for name, _path in self.project.catalog_paths],
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
            allowed_next_actions=["flow.plan", "run.inspect"],
        )

    def plan_flow(
        self,
        *,
        owner: str,
        flow: str,
        target: str,
        profile: str | None,
    ) -> dict[str, Any]:
        """Compile one catalog-selected Flow through the existing FlowEngine."""

        resolved = ProjectFlow(
            self.project,
            owner,
        ).plan(
            flow=flow,
            target=target,
            profile=profile,
        )
        plan = resolved.plan
        record = resolved.engine.plan_record(plan)
        return self.response(
            operation="flow.plan",
            authority="plan",
            conclusion="planned",
            summary=(
                f"Resolved {plan.spec.flow_id}/{plan.target.target_id} into "
                f"{len(plan.nodes)} typed "
                "nodes; no backend was executed."
            ),
            data={
                "plan_identity": resolved.plan_identity,
                "plan": _public_value(record),
            },
            resources=[
                self.project_resource_uri,
                self.owner_resource_uri(plan.spec.owner),
            ],
            allowed_next_actions=["project.inspect", "review-plan"],
        )

    def plan_campaign(self, *, campaign_json: str) -> dict[str, Any]:
        resolved = resolve_project_design_campaign(self.project, campaign_json)
        campaign = resolved.campaign
        return self.response(
            operation="campaign.plan",
            authority="plan",
            conclusion="planned",
            summary=(
                f"Compiled bounded Design Campaign {campaign.campaign_id!r} with "
                "one explicit baseline attempt; no backend was executed."
            ),
            data={
                "campaign_identity": resolved.identity,
                "plan": resolved.record,
            },
            resources=[
                self.project_resource_uri,
                self.owner_resource_uri(campaign.owner),
            ],
            allowed_next_actions=["review-campaign", "campaign.run"],
        )

    def inspect_run(
        self,
        *,
        owner: str,
        flow: str,
        target: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Read one exact persisted Flow result after resolving its owner contract."""

        selected_owner = self._owner(owner)
        flow_name = identifier(flow, "Flow identity")
        target_name = identifier(target, "Flow target")
        identity = run_identity(run_id)
        project_flow = ProjectFlow(self.project, selected_owner.name)
        managed = AgenticRunStore(
            self.project.artifact_root,
            self.project_id,
        )
        managed_paths = managed.paths(
            owner=selected_owner.name,
            flow=flow_name,
            target=target_name,
            run_id=identity,
        )
        state_path = managed_paths.role("control") / "state.json"
        if state_path.exists():
            state = managed.read_state(managed_paths)
            if (
                state["owner"] != selected_owner.name
                or state["flow"] != flow_name
                or state["target"] != target_name
                or state["run_id"] != identity
            ):
                raise ValueError("managed Flow Run identity drift")
            result: dict[str, Any] | None = None
            try:
                result = project_flow.read_result(
                    flow=flow_name,
                    target=target_name,
                    run_id=identity,
                )
            except (OSError, RuntimeError):
                if state["status"] not in RUNNING_STATUSES:
                    raise
            resource = self.run_resource_uri(
                owner=selected_owner.name,
                flow=flow_name,
                target=target_name,
                run_id=identity,
            )
            return self.response(
                operation="run.inspect",
                authority=(
                    "managed-run-state"
                    if result is None
                    else "recorded-flow-result"
                ),
                conclusion=(
                    "in-progress"
                    if state["status"] in RUNNING_STATUSES
                    else state["status"]
                ),
                summary=(
                    f"Managed Flow Run {identity} is {state['status']!r}; "
                    "no additional qualification claim was made."
                ),
                data={
                    "management": state,
                    "result": None if result is None else _public_value(result),
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
        result = project_flow.read_result(
            flow=flow_name,
            target=target_name,
            run_id=identity,
        )
        resource = self.run_resource_uri(
            owner=selected_owner.name,
            flow=flow_name,
            target=target_name,
            run_id=identity,
        )
        return self.response(
            operation="run.inspect",
            authority="recorded-flow-result",
            conclusion="recorded",
            summary=(
                f"Read the identity-matched Flow result with status "
                f"{result.get('status', 'unknown')!r}; no qualification claim was added."
            ),
            data={
                "result": _public_value(result),
                "model_context": {
                    "record_text_trust": "untrusted",
                    "qualification_authority": "record-only",
                },
            },
            resources=[resource],
            allowed_next_actions=["project.inspect", "run.inspect"],
        )

    def validate_candidate(
        self,
        *,
        owner: str,
        candidate_json: str,
        artifact_json: tuple[str, ...],
    ) -> dict[str, Any]:
        """Validate an immutable Candidate chain inside one cataloged owner."""

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

    def _flows(
        self,
        owner: RepositoryOwner,
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    ) -> list[dict[str, Any]]:
        snapshots = self.project.owner_flow_catalog_snapshots(
            owner,
            inventory=catalog_inventory,
        )
        if not snapshots:
            return []
        if len(snapshots) != 1:
            raise ValueError(
                f"cataloged owner {owner.name!r} must select exactly one Flow Catalog"
            )
        catalog = parse_flow_catalog(
            snapshots[0].document,
            snapshots[0].path,
            owner_root=owner.root,
        )
        flows: list[dict[str, Any]] = []
        for entry in catalog.entries:
            selection = resolve_catalog_selection(
                catalog,
                flow_id=entry.flow_id,
            )
            flows.append(
                {
                    "name": entry.flow_id,
                    "default_profile": entry.default_profile,
                    "profiles": sorted(entry.profiles),
                    "targets": sorted(
                        target.target_id for target in selection.spec.targets
                    ),
                }
            )
        return sorted(flows, key=lambda item: item["name"])

    def _design_targets(
        self,
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    ) -> tuple[Any, ...]:
        return load_design_target_catalog(
            project=self.project,
            catalog_inventory=catalog_inventory,
        ).targets

    def _layout_targets(
        self,
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
    ) -> tuple[Any, ...]:
        return load_layout_target_catalog(
            project=self.project,
            catalog_inventory=catalog_inventory,
        ).targets

    def _owner_payload(
        self,
        owner: RepositoryOwner,
        design_targets: tuple[Any, ...],
        layout_targets: tuple[Any, ...],
        catalog_inventory: tuple[OwnerCatalogSnapshot, ...],
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
            "flows": self._flows(owner, catalog_inventory),
            "design_targets": [
                {
                    "name": target.name,
                    "description": target.description,
                    "spec": (
                        None
                        if target.spec_relative is None
                        else target.spec_relative.as_posix()
                    ),
                    "modes": [mode.name for mode in target.modes],
                }
                for target in design_targets
                if target.owner == owner.name
            ],
            "layout_targets": [
                {
                    "name": target.name,
                    "description": target.description,
                    "spec": target.spec_relative.as_posix(),
                    "actions": list(target.actions),
                }
                for target in layout_targets
                if target.owner == owner.name
            ],
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
        return {
            "schema": 1,
            "contract_kind": READ_RESULT_KIND,
            "operation": operation,
            "project_id": self.project_id,
            "authority": authority,
            "conclusion": conclusion,
            "summary": summary,
            "data": _public_value(data),
            "resources": resources,
            "allowed_next_actions": allowed_next_actions,
        }


__all__ = [
    "AgenticReadInterface",
    "READ_RESULT_KIND",
]
