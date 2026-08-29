"""Client-neutral read and planning Interface for agent integrations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from sigilicon.domain.circuit_design import (
    design_artifact_from_json,
    design_candidate_from_json,
    design_decision_from_json,
)
from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.flow import (
    FlowContractError,
    FlowEngine,
    FlowPlan,
    load_catalog_selection,
    load_flow_catalog,
)
from sigilicon.flow.model import identifier, owner_identity, run_identity
from sigilicon.paths import ProjectContext
from sigilicon.workflows.project_flow import (
    ProjectFlow,
    project_workflow_registry,
)
from sigilicon.workflows.design_artifacts import DesignArtifactInterface
from sigilicon.workflows.design_campaign import (
    DesignCampaign,
    DesignCampaignAttempt,
    DesignCampaignContinuation,
    DesignCampaignRunner,
    DesignCampaignSpec,
    design_campaign_spec_from_json,
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
class ResolvedAgenticFlowPlan:
    """Compatibility view of a ProjectFlow plan for agentic workflows."""

    engine: FlowEngine
    plan: FlowPlan
    plan_identity: str


@dataclass(frozen=True)
class ResolvedAgenticCampaignPlan:
    engine: FlowEngine
    campaign: DesignCampaign
    campaign_identity: str
    plan_record: dict[str, Any]
    source: DesignCampaignSpec


@dataclass(frozen=True)
class AgenticReadInterface:
    """Resolve only cataloged project identities through existing domain Modules."""

    repository: Project
    project_id: str

    def __post_init__(self) -> None:
        if self.project_id != _repository_identity(self.repository):
            raise ValueError("agentic project identity drift")

    @classmethod
    def from_project_root(cls, project_root: Path | str) -> "AgenticReadInterface":
        return cls.from_project(Project.from_project_root(project_root))

    @classmethod
    def from_project(cls, project: Project) -> "AgenticReadInterface":
        return cls(project, _repository_identity(project))

    @classmethod
    def from_project_context(
        cls,
        project: ProjectContext,
    ) -> "AgenticReadInterface":
        repository = Project.from_file(
            project.project_root / "sigilicon.toml"
        )
        if (
            repository.project_root != project.project_root
            or repository.workspace_root != project.workspace_root
            or repository.artifact_root != project.artifact_root
        ):
            raise ValueError("agentic read context disagrees with sigilicon.toml")
        return cls.from_project(repository)

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
            self.repository.owners
            if owner is None
            else (self._owner(owner),)
        )
        design_targets = self._design_targets()
        layout_targets = self._layout_targets()
        owners = [
            self._owner_payload(item, design_targets, layout_targets)
            for item in selected
        ]
        source = inspect_source_state(self.repository.project_root)
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
                "catalogs": [name for name, _path in self.repository.catalog_paths],
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

        resolved = self.resolve_flow_plan(
            owner=owner,
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

    def resolve_flow_plan(
        self,
        *,
        owner: str,
        flow: str,
        target: str,
        profile: str | None,
    ) -> ResolvedAgenticFlowPlan:
        """Resolve one exact catalog plan for peer application Interfaces."""

        flow_name = identifier(flow, "Flow identity")
        target_name = identifier(target, "Flow target")
        profile_name = (
            None
            if profile is None
            else identifier(profile, "Execution Profile identity")
        )
        planned = ProjectFlow(
            self.repository,
            owner,
            project_workflow_registry,
        ).plan(
            flow=flow_name,
            target=target_name,
            profile=profile_name,
        )
        return ResolvedAgenticFlowPlan(
            planned.engine,
            planned.plan,
            planned.plan_identity,
        )

    def resolve_plan_identity(self, plan_identity: str) -> ResolvedAgenticFlowPlan:
        """Recompile project catalogs and find one uniquely matching Plan identity."""

        if not isinstance(plan_identity, str) or not plan_identity:
            raise ValueError("Flow Plan identity must be non-empty text")
        matches: list[ResolvedAgenticFlowPlan] = []
        combinations = 0
        for owner in self.repository.owners:
            catalogs = self._flow_catalogs(owner)
            if not catalogs:
                continue
            if len(catalogs) != 1:
                raise ValueError(
                    f"cataloged owner {owner.name!r} must select exactly one Flow Catalog"
                )
            catalog_path = catalogs[0]
            catalog = load_flow_catalog(catalog_path, owner_root=owner.root)
            for entry in catalog.entries:
                profiles = tuple(sorted({entry.default_profile, *entry.profiles}))
                for profile in profiles:
                    selection = load_catalog_selection(
                        catalog_path,
                        owner_root=owner.root,
                        flow_id=entry.flow_id,
                        profile_id=profile,
                    )
                    for target in selection.spec.targets:
                        combinations += 1
                        if combinations > 10_000:
                            raise ValueError("project exposes too many executable Flow plans")
                        try:
                            resolved = self.resolve_flow_plan(
                                owner=owner.name,
                                flow=entry.flow_id,
                                target=target.target_id,
                                profile=profile,
                            )
                        except FlowContractError:
                            # Project-owned Adapter extensions are not globally
                            # available. They cannot match a plan compiled by
                            # this server's current owner registry.
                            continue
                        if resolved.plan_identity == plan_identity:
                            matches.append(resolved)
        if len(matches) != 1:
            raise ValueError("Flow Plan identity is unknown or ambiguous in this project")
        return matches[0]

    def resolve_campaign_plan(
        self,
        campaign_json: str,
    ) -> ResolvedAgenticCampaignPlan:
        """Compile strict semantic Flow selectors into one bounded Campaign."""

        source = design_campaign_spec_from_json(campaign_json)
        self._owner(source.owner)
        source_baseline = source.baseline
        resolved = self.resolve_flow_plan(
            owner=source.owner,
            flow=source_baseline.flow,
            target=source_baseline.target,
            profile=source_baseline.profile,
        )
        engine = resolved.engine
        baseline = DesignCampaignAttempt(
            source_baseline.iteration_id,
            resolved.plan,
            source_baseline.candidate,
            source_baseline.artifacts,
            source_baseline.stages,
            None,
        )
        continuation = None
        if source.continuation is not None:
            template = source.continuation
            resolved = self.resolve_flow_plan(
                owner=source.owner,
                flow=template.flow,
                target=template.target,
                profile=template.profile,
            )
            continuation = DesignCampaignContinuation(
                resolved.plan,
                template.candidate,
                template.artifacts,
                template.stages,
                template.proposal_node,
                template.repair_policy,
            )
        campaign = DesignCampaign(
            source.owner,
            source.campaign_id,
            baseline,
            source.budget,
            source.scope,
            continuation,
        )
        runner = DesignCampaignRunner(
            engine,
            artifact_root=self.repository.artifact_root,
        )
        record = runner.plan_record(campaign)
        return ResolvedAgenticCampaignPlan(
            engine,
            campaign,
            runner.campaign_identity(campaign),
            record,
            source,
        )

    def plan_campaign(self, *, campaign_json: str) -> dict[str, Any]:
        resolved = self.resolve_campaign_plan(campaign_json)
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
                "campaign_identity": resolved.campaign_identity,
                "plan": resolved.plan_record,
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
        catalog = self._flow_catalog(selected_owner)
        selection = load_catalog_selection(
            catalog,
            owner_root=selected_owner.root,
            flow_id=flow_name,
        )
        selection.spec.target(target_name)
        engine = FlowEngine(
            project_workflow_registry(self.repository, selected_owner.root)
        )
        managed = AgenticRunStore(
            self.repository.artifact_root,
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
                result = engine.read_run_result(
                    artifact_root=self.repository.artifact_root,
                    owner=selected_owner.name,
                    flow_id=flow_name,
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
        result = engine.read_run_result(
            artifact_root=self.repository.artifact_root,
            owner=selected_owner.name,
            flow_id=flow_name,
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
        validation = DesignArtifactInterface().validate_candidate(
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
        return self.repository.owner(name)

    def _flow_catalog(self, owner: RepositoryOwner) -> Path:
        return self.repository.owner_flow_catalog(owner)

    def _flows(self, owner: RepositoryOwner) -> list[dict[str, Any]]:
        matches = self._flow_catalogs(owner)
        if not matches:
            return []
        if len(matches) != 1:
            raise ValueError(
                f"cataloged owner {owner.name!r} must select exactly one Flow Catalog"
            )
        catalog_path = matches[0]
        catalog = load_flow_catalog(catalog_path, owner_root=owner.root)
        flows: list[dict[str, Any]] = []
        for entry in catalog.entries:
            selection = load_catalog_selection(
                catalog_path,
                owner_root=owner.root,
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

    def _flow_catalogs(self, owner: RepositoryOwner) -> tuple[Path, ...]:
        return self.repository.owner_flow_catalogs(owner)

    def _design_targets(self) -> tuple[Any, ...]:
        if not self.repository.flow_catalogs("design_targets"):
            return ()
        return load_design_target_catalog(project=self.repository).targets

    def _layout_targets(self) -> tuple[Any, ...]:
        if not self.repository.flow_catalogs("layout_targets"):
            return ()
        return load_layout_target_catalog(project=self.repository).targets

    def _owner_payload(
        self,
        owner: RepositoryOwner,
        design_targets: tuple[Any, ...],
        layout_targets: tuple[Any, ...],
    ) -> dict[str, Any]:
        root = self.repository.project_root
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
            "flows": self._flows(owner),
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
    "ResolvedAgenticCampaignPlan",
    "ResolvedAgenticFlowPlan",
]
