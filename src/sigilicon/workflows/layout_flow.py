"""Project-bound Adapters for planned custom-layout generation and verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from sigilicon.flow import (
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
    SourceMember,
)
from sigilicon.flow.layout import (
    LAYOUT_GENERATION_ACTION,
    LAYOUT_GENERATION_EVIDENCE_KIND,
    LAYOUT_VERIFICATION_ACTION,
    LAYOUT_VERIFICATION_EVIDENCE_KIND,
)
from sigilicon.flow.source_assets import source_member_matches
from sigilicon.layout.spec import resolve_layout_spec
from sigilicon.workflows.layout_generation import (
    LayoutPlanningResult,
    generate_layout,
)
from sigilicon.workflows.layout_targets import LayoutTargetCatalog
from sigilicon.workflows.layout_verification import verify_layout
from sigilicon.workflows.run_artifacts import FlowRunArtifacts
from sigilicon.workflows.source_control import artifact_source_state


class ProjectLayoutTargetAdapter:
    """Execute one exact owner layout plan inside its Flow Action lifecycle."""

    def __init__(
        self,
        project: Any,
        owner: str,
        catalog: LayoutTargetCatalog,
        *,
        target: str,
        operation: str,
        planning: LayoutPlanningResult,
        client_factory: Callable[[], Any],
        sources: tuple[SourceMember, ...] | None = None,
    ) -> None:
        self._project = project
        self._owner = project.owner(owner)
        self._catalog = catalog
        self._target = catalog.get(target)
        self._route_operation = operation
        self._planning = planning
        self._client_factory = client_factory
        self._sources = (
            catalog.source_members_for(self._target, planning)
            if sources is None
            else sources
        )

    @property
    def sources(self) -> tuple[SourceMember, ...]:
        return self._sources

    def run(self, context: ActionContext) -> AdapterResult:
        scope = context.require_project_scope()
        if (
            scope.owner != self._owner.name
            or scope.owner_root != self._owner.root
            or scope.project.project_root != self._project.project_root
        ):
            raise FlowExecutionError("layout Action project owner scope drift")
        expected_action = (
            LAYOUT_GENERATION_ACTION
            if self._route_operation == "generate"
            else LAYOUT_VERIFICATION_ACTION
        )
        if context.action.kind != expected_action:
            raise FlowExecutionError("layout route selected a different Action kind")
        if self._text(context, "target") != self._target.name:
            raise FlowExecutionError("layout Action target drift")
        operation = self._text(context, "operation")
        allowed_operations = (
            {"generate"}
            if self._route_operation == "generate"
            else (
                {"verify-drc", "verify-lvs"}
                if self._route_operation == "verify-all"
                else {self._route_operation}
            )
        )
        if operation not in allowed_operations:
            raise FlowExecutionError("layout Action operation drift")
        try:
            if not all(source_member_matches(member) for member in self._sources):
                raise FlowExecutionError(
                    "owner layout source changed after Flow planning"
                )
            resolve_layout_spec(
                self._target.spec,
                project=self._project,
                snapshot=self._planning.spec,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, FlowExecutionError):
                raise
            raise FlowExecutionError(
                f"owner layout source changed after Flow planning: {exc}"
            ) from exc
        if context.operation_id is None:
            raise FlowExecutionError("layout Action has no managed operation")
        source = artifact_source_state(self._project.project_root)
        source["layout_route"] = [
            {
                "path": member.path,
                "sha256": hashlib.sha256(
                    member.record_text.encode("utf-8")
                ).hexdigest(),
                "executable": member.executable,
            }
            for member in self._sources
        ]
        artifacts = FlowRunArtifacts(context, "evidence", source)
        for index, member in enumerate(self._sources):
            artifacts.write_text(
                "inputs",
                ("selected-sources", f"{index:03d}-{Path(member.path).name}"),
                member.record_text,
                label="exact planned layout source",
            )
        if self._route_operation == "generate":
            return self._generate(context, artifacts)
        return self._verify(context, artifacts)

    def _generate(
        self,
        context: ActionContext,
        artifacts: FlowRunArtifacts,
    ) -> AdapterResult:
        if set(context.action_config) != {"target", "operation"}:
            raise FlowExecutionError("layout generation Action configuration drift")
        if set(context.adapter_config) != {"timeout_seconds"}:
            raise FlowExecutionError("layout generation Adapter configuration drift")
        timeout = self._positive_timeout(context, "timeout_seconds")
        result = generate_layout(
            self._planning,
            self._client_factory(),
            artifacts=artifacts,
            operation_id=context.operation_id,
            bind_operation=context.bind_workspace_operation,
            timeout=timeout,
        )
        payload = {
            "target": self._target.name,
            "operation": "generate",
            "library": self._planning.spec.library,
            "cell": self._planning.spec.cell,
            "view": self._planning.spec.view,
            "passed": True,
            "execution_completed": True,
            "instance_count": result.instance_count,
            "product_qualification_conclusion": False,
        }
        evidence = artifacts.write_json(
            "outputs",
            ("flow-evidence.json",),
            payload,
            label="typed layout generation evidence",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence", LAYOUT_GENERATION_EVIDENCE_KIND, evidence
                    ),
                ),
                facts={
                    "passed": True,
                    "execution-completed": True,
                    "instance-count": result.instance_count,
                    "product-qualification-conclusion": False,
                },
                details=payload,
            )
        )

    def _verify(
        self,
        context: ActionContext,
        artifacts: FlowRunArtifacts,
    ) -> AdapterResult:
        allowed = {
            "target",
            "operation",
            "check",
            "evidence_role",
            "evidence_level",
            "evidence_scope",
        }
        if set(context.action_config) != allowed:
            raise FlowExecutionError("layout verification Action configuration drift")
        if set(context.adapter_config) != {
            "xstream_timeout_seconds",
            "calibre_timeout_seconds",
        }:
            raise FlowExecutionError("layout verification Adapter configuration drift")
        check = self._text(context, "check")
        operation = self._text(context, "operation")
        if operation != f"verify-{check}" or check not in {"drc", "lvs"}:
            raise FlowExecutionError("layout verification check disagrees with route")
        xstream = context.capabilities["tool.cadence-xstream"].executable
        calibre = context.capabilities["tool.calibre"].executable
        if xstream is None or calibre is None:
            raise FlowExecutionError(
                "layout verification tool capabilities require executable paths"
            )
        result = verify_layout(
            self._planning,
            self._client_factory(),
            check=check,
            artifacts=artifacts,
            operation_id=context.operation_id,
            bind_operation=context.bind_workspace_operation,
            xstream=xstream,
            calibre=calibre,
            xstream_timeout=self._positive_timeout(
                context, "xstream_timeout_seconds"
            ),
            calibre_timeout=self._positive_timeout(
                context, "calibre_timeout_seconds"
            ),
        )
        envelope = context.require_evidence()
        metadata = {
            "evidence_role": envelope.role,
            "evidence_level": envelope.level,
            "evidence_scope": envelope.scope,
        }
        payload = {
            "target": self._target.name,
            "operation": operation,
            "library": self._planning.spec.library,
            "cell": self._planning.spec.cell,
            "view": self._planning.spec.view,
            "check": check,
            "passed": result.passed,
            "execution_completed": True,
            **metadata,
            "physical_verification_evidence": json.loads(
                result.evidence.canonical_json()
            ),
            "product_qualification_conclusion": False,
        }
        evidence = artifacts.write_json(
            "outputs",
            ("flow-evidence.json",),
            payload,
            label="typed layout verification evidence",
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence", LAYOUT_VERIFICATION_EVIDENCE_KIND, evidence
                    ),
                ),
                facts={
                    "passed": result.passed,
                    "execution-completed": True,
                    "check": check,
                    "evidence-role": metadata["evidence_role"],
                    "evidence-level": metadata["evidence_level"],
                    "evidence-scope": metadata["evidence_scope"],
                    "product-qualification-conclusion": False,
                },
                details=payload,
            )
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


__all__ = ["ProjectLayoutTargetAdapter"]
