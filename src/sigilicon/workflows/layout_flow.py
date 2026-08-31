"""Stateless Adapters for explicitly planned custom-layout Actions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from sigilicon.flow import (
    ActionPlan,
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
    SourceMember,
    FlowNode,
)
from sigilicon.flow.layout import (
    LAYOUT_ACTION_PLAN,
    LAYOUT_GENERATION_ADAPTER,
    LAYOUT_GENERATION_ACTION,
    LAYOUT_GENERATION_EVIDENCE_KIND,
    LAYOUT_VERIFICATION_ACTION,
    LAYOUT_VERIFICATION_EVIDENCE_KIND,
)
from sigilicon.flow.registry import FlowRegistry
from sigilicon.flow.evidence import FactSet, FactSource
from sigilicon.flow.serialization import json_value
from sigilicon.flow.source_assets import (
    source_member_matches,
)
from sigilicon.domain.repository import Project, RepositoryOwner
from sigilicon.workflows.layout_generation import (
    LayoutPlanningResult,
    generate_layout,
    plan_layout_spec,
)
from sigilicon.workflows.run_artifacts import FlowRunArtifacts
from sigilicon.workflows.source_closure import project_source_members
from sigilicon.workflows.source_control import artifact_source_state


_LAYOUT_OPERATIONS = frozenset({"generate", "verify-drc", "verify-lvs"})
_LAYOUT_CONFIG_FIELDS = frozenset({"target", "operation", "spec"})
_LAYOUT_VERIFICATION_FIELDS = frozenset(
    {
        "target",
        "operation",
        "spec",
        "check",
        "evidence_role",
        "evidence_level",
        "evidence_scope",
    }
)


def _required_text(config: Mapping[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"layout Action {field!r} must be non-empty text")
    return value


def _fact_set(
    context: ActionContext,
    values: Mapping[str, object],
) -> FactSet:
    """Project one typed layout observation through the Action schema."""

    schema = context.action.fact_schema
    if schema is None:
        raise FlowExecutionError(f"Action {context.node_id!r} has no fact schema")
    return FactSet(
        schema,
        values,
        FactSource(context.action.kind, context.node_id),
    )


def _planned_product_conclusion(context: ActionContext) -> bool:
    """Do not promote a backend payload to product qualification authority."""

    if context.evidence is not None:
        context.require_evidence()
    return False


def _relative_spec(
    config: Mapping[str, Any],
    project: Project,
    owner: RepositoryOwner,
) -> tuple[str, Path]:
    """Resolve the one project-relative cell spec owned by *owner*."""

    value = _required_text(config, "spec")
    path, relative = project.resolve_owner_file(owner, value, "layout Action spec")
    return relative.as_posix(), path


def _layout_source_members(
    project: Project,
    planning: LayoutPlanningResult,
) -> tuple[SourceMember, ...]:
    """Turn one retained layout snapshot into its complete source closure.

    ``LayoutPlanningResult.source_records`` is the authority for bytes.  The
    closure includes project files and installed Sigilicon package files, while
    rejecting an undeclared third root instead of silently re-reading it.
    """

    records = planning.source_records
    if not isinstance(records, Mapping) or not records:
        raise ValueError("layout planning result must retain source records")

    return project_source_members(
        project,
        records,
        label="layout",
        records=records,
    )


def _validate_layout_config(config: Mapping[str, Any]) -> tuple[str, str, str]:
    """Validate the direct layout node fields and return target/op/spec."""

    if not isinstance(config, Mapping):
        raise ValueError("layout Action configuration must be a mapping")
    operation = _required_text(config, "operation")
    fields = (
        _LAYOUT_CONFIG_FIELDS
        if operation == "generate"
        else _LAYOUT_VERIFICATION_FIELDS
    )
    if operation not in _LAYOUT_OPERATIONS:
        raise ValueError(
            "layout Action operation must be one of "
            f"{sorted(_LAYOUT_OPERATIONS)}"
        )
    if set(config) != fields:
        raise ValueError(
            "layout Action configuration fields must be "
            f"{sorted(fields)}"
        )
    target = _required_text(config, "target")
    spec = _required_text(config, "spec")
    if operation != "generate":
        check = _required_text(config, "check")
        if check not in {"drc", "lvs"} or operation != f"verify-{check}":
            raise ValueError("layout verification operation and check disagree")
        _required_text(config, "evidence_role")
        _required_text(config, "evidence_level")
        _required_text(config, "evidence_scope")
    return target, operation, spec


def plan_layout_action(
    project: Project,
    owner: RepositoryOwner,
    config: Mapping[str, Any],
) -> ActionPlan:
    """Plan one custom-layout node directly from its typed node config.

    The caller supplies the owner target/recipe node configuration.  No target
    registry or expanded Flow is consulted here.  Planning resolves the
    owner-relative spec once and retains the exact project/package source
    records that produced the typed layout plan.
    """

    if not isinstance(project, Project):
        raise ValueError("layout Action planning requires an explicit Project")
    if owner not in project.owners:
        raise ValueError(
            f"repository does not contain owner {owner.name!r}"
        )
    target, operation, _ = _validate_layout_config(config)
    spec_relative, spec_path = _relative_spec(config, project, owner)
    planning = plan_layout_spec(spec_path, project=project)
    if planning.spec.path != spec_path or planning.spec.project is not project:
        raise ValueError(
            "layout planning result does not belong to this Project/spec"
        )
    source_members = _layout_source_members(project, planning)
    invocation = LayoutInvocation(
        target=target,
        operation=operation,
        spec=spec_relative,
        planning=planning,
    )
    return ActionPlan(
        LAYOUT_ACTION_PLAN,
        invocation,
        invocation.as_dict(),
        source_members,
    )


@dataclass(frozen=True)
class LayoutInvocation:
    """Layout-specific payload inside the common source-bound ActionPlan."""

    target: str
    operation: str
    spec: str
    planning: LayoutPlanningResult

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("layout Action target must be non-empty text")
        if (
            not isinstance(self.operation, str)
            or self.operation not in _LAYOUT_OPERATIONS
        ):
            raise ValueError(
                f"unsupported layout Action operation: {self.operation!r}"
            )
        if not isinstance(self.spec, str) or not self.spec:
            raise ValueError("layout Action spec must be non-empty text")
        relative = PurePosixPath(self.spec)
        if (
            relative.is_absolute()
            or "\\" in self.spec
            or relative.as_posix() != self.spec
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError(
                "layout Action spec must be a canonical project-relative path"
            )
        if not isinstance(self.planning, LayoutPlanningResult):
            raise ValueError(
                "layout Action plan must contain a typed planning result"
            )
        expected_path = (
            self.planning.spec.project_root.joinpath(*relative.parts).resolve()
        )
        if expected_path != self.planning.spec.path:
            raise ValueError("layout Action spec disagrees with its planning result")
        records = self.planning.source_records
        if not isinstance(records, Mapping) or not records:
            raise ValueError(
                "layout Action plan must contain retained source records"
            )
        expected_records: dict[Path, str] = {}
        for path, record in records.items():
            if not isinstance(path, (str, Path)) or not isinstance(record, str):
                raise ValueError("layout Action source records are invalid")
            resolved = Path(path).resolve()
            previous = expected_records.get(resolved)
            if previous is not None and previous != record:
                raise ValueError(
                    "layout Action source records contain duplicate path drift"
                )
            expected_records[resolved] = record

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "operation": self.operation,
            "spec": self.spec,
            "layout": json.loads(self.planning.plan.canonical_json()),
        }


class LayoutActionAdapter:
    """Execute one exact owner layout plan inside its Flow Action lifecycle."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
    ) -> None:
        self._client_factory = client_factory

    def run(self, context: ActionContext) -> AdapterResult:
        selected = context.require_action_plan(
            LAYOUT_ACTION_PLAN,
            LayoutInvocation,
        )
        assert context.action_plan is not None
        if json_value(context.action_plan.record) != selected.as_dict():
            raise FlowExecutionError("typed layout Action Plan record drift")
        planning = selected.planning
        project = planning.spec.project
        owner = project.require_owner(planning.spec.path)
        scope = context.require_project_scope()
        if (
            scope.owner != owner.name
            or scope.owner_root != owner.root
            or scope.project.project_root != project.project_root
        ):
            raise FlowExecutionError("layout Action Plan project owner scope drift")
        expected_action = (
            LAYOUT_GENERATION_ACTION
            if selected.operation == "generate"
            else LAYOUT_VERIFICATION_ACTION
        )
        if context.action.kind != expected_action:
            raise FlowExecutionError("layout Action selected a different Action kind")
        self._validate_action_config(context, selected)
        self._validate_source_closure(context, selected)
        if context.operation_id is None:
            raise FlowExecutionError("layout Action has no managed operation")
        source = artifact_source_state(project.project_root)
        source["layout_route"] = [
            {
                "path": member.path,
                "sha256": hashlib.sha256(
                    member.record_text.encode("utf-8")
                ).hexdigest(),
                "executable": member.executable,
            }
            for member in context.action_plan.sources
        ]
        artifacts = FlowRunArtifacts(context, "evidence", source)
        for index, member in enumerate(context.action_plan.sources):
            artifacts.write_text(
                "inputs",
                ("selected-sources", f"{index:03d}-{Path(member.path).name}"),
                member.record_text,
            )
        if selected.operation == "generate":
            return self._generate(context, selected, artifacts)
        return self._verify(context, selected, artifacts)

    @classmethod
    def _validate_action_config(
        cls,
        context: ActionContext,
        selected: LayoutInvocation,
    ) -> None:
        config = context.action_config
        expected_fields = (
            _LAYOUT_CONFIG_FIELDS
            if selected.operation == "generate"
            else _LAYOUT_VERIFICATION_FIELDS
        )
        if set(config) != expected_fields:
            raise FlowExecutionError(
                "layout Action configuration fields do not match typed plan"
            )
        if cls._text(context, "target") != selected.target:
            raise FlowExecutionError("layout Action target drift")
        if cls._text(context, "operation") != selected.operation:
            raise FlowExecutionError("layout Action operation drift")
        if cls._text(context, "spec") != selected.spec:
            raise FlowExecutionError("layout Action spec drift")
        if selected.operation == "generate":
            return
        check = cls._text(context, "check")
        if check not in {"drc", "lvs"} or selected.operation != f"verify-{check}":
            raise FlowExecutionError("layout verification operation and check disagree")
        evidence = context.require_evidence()
        if (
            evidence.role != cls._text(context, "evidence_role")
            or evidence.level != cls._text(context, "evidence_level")
            or evidence.scope != cls._text(context, "evidence_scope")
        ):
            raise FlowExecutionError(
                "layout verification evidence envelope disagrees with Action config"
            )

    @staticmethod
    def _validate_source_closure(
        context: ActionContext,
        selected: LayoutInvocation,
    ) -> None:
        if context.action_plan is None:
            raise FlowExecutionError("layout Action is missing its typed plan")
        expected = {
            Path(path).resolve(): record
            for path, record in selected.planning.source_records.items()
        }
        provided = {
            member.location: member.record_text
            for member in context.action_plan.sources
        }
        if provided != expected:
            raise FlowExecutionError("typed layout Action Plan source closure drift")
        try:
            if not all(
                source_member_matches(member)
                for member in context.action_plan.sources
            ):
                raise FlowExecutionError(
                    "owner layout source changed after Flow planning"
                )
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            if isinstance(exc, FlowExecutionError):
                raise
            raise FlowExecutionError(
                f"owner layout source changed after Flow planning: {exc}"
            ) from exc

    def _generate(
        self,
        context: ActionContext,
        selected: LayoutInvocation,
        artifacts: FlowRunArtifacts,
    ) -> AdapterResult:
        if set(context.action_config) != _LAYOUT_CONFIG_FIELDS:
            raise FlowExecutionError("layout generation Action configuration drift")
        if set(context.adapter_config) != {"timeout_seconds"}:
            raise FlowExecutionError("layout generation Adapter configuration drift")
        timeout = self._positive_timeout(context, "timeout_seconds")
        result = generate_layout(
            selected.planning,
            self._client_factory(),
            artifacts=artifacts,
            operation_id=context.operation_id,
            bind_operation=context.bind_workspace_operation,
            timeout=timeout,
        )
        product_conclusion = _planned_product_conclusion(context)
        payload = {
            "target": selected.target,
            "operation": "generate",
            "library": selected.planning.spec.library,
            "cell": selected.planning.spec.cell,
            "view": selected.planning.spec.view,
            "passed": True,
            "instance_count": result.instance_count,
            "product_qualification_conclusion": product_conclusion,
        }
        evidence = artifacts.write_json(
            "outputs",
            ("flow-evidence.json",),
            payload,
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence", LAYOUT_GENERATION_EVIDENCE_KIND, evidence
                    ),
                ),
                facts=_fact_set(context, {
                    "passed": True,
                    "instance-count": result.instance_count,
                    "product-qualification-conclusion": product_conclusion,
                }),
            )
        )

    def _verify(
        self,
        context: ActionContext,
        selected: LayoutInvocation,
        artifacts: FlowRunArtifacts,
    ) -> AdapterResult:
        del context, selected, artifacts
        raise FlowExecutionError(
            "combined XStream/Calibre layout verification is experimental"
        )

    @staticmethod
    def _text(context: ActionContext, name: str) -> str:
        value = context.action_config.get(name)
        if not isinstance(value, str) or not value:
            raise FlowExecutionError(
                f"layout Action {name!r} must be non-empty text"
            )
        return value

    @staticmethod
    def _positive_timeout(context: ActionContext, name: str) -> int:
        value = context.adapter_config.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FlowExecutionError(
                f"layout Adapter {name!r} must be a positive integer"
            )
        return value


def install_layout_flow(
    registry: FlowRegistry,
    project: Project,
    owner: RepositoryOwner,
    *,
    client_factory: Callable[[], Any],
) -> None:
    """Install custom-layout planners and the native layout Adapter."""

    registry.register_adapter_factory(
        LAYOUT_GENERATION_ADAPTER,
        lambda: LayoutActionAdapter(client_factory=client_factory),
    )

    def planner(node: FlowNode) -> ActionPlan:
        return plan_layout_action(project, owner, node.config)

    registry.register_action_planner(LAYOUT_GENERATION_ACTION, planner)
    registry.register_action_planner(LAYOUT_VERIFICATION_ACTION, planner)


__all__ = [
    "LayoutActionAdapter",
    "LayoutInvocation",
    "install_layout_flow",
    "plan_layout_action",
]
