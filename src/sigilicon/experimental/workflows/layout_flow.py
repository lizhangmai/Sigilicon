"""Experimental Adapter for the custom-layout XStream/Calibre pilot."""

from __future__ import annotations

import json

from sigilicon.experimental.workflows.layout_verification import (
    run_experimental_layout_verification,
)
from sigilicon.flow import (
    ActionContext,
    AdapterResult,
    CollectedActionResult,
    FlowExecutionError,
    ProducedArtifact,
)
from sigilicon.flow.evidence import FactSet, FactSource
from sigilicon.flow.layout import (
    LAYOUT_VERIFICATION_EVIDENCE_KIND,
)
from sigilicon.workflows.layout_flow import (
    LayoutActionAdapter,
    LayoutInvocation,
)
from sigilicon.workflows.run_artifacts import FlowRunArtifacts


class ExperimentalLayoutActionAdapter(LayoutActionAdapter):
    """Run custom-layout verification through the opt-in XStream/Calibre path."""

    def _verify(
        self,
        context: ActionContext,
        selected: LayoutInvocation,
        artifacts: FlowRunArtifacts,
    ) -> AdapterResult:
        allowed = {
            "target",
            "operation",
            "spec",
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
        result = run_experimental_layout_verification(
            selected.planning,
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
        evidence = artifacts.write_json(
            "outputs",
            ("flow-evidence.json",),
            {
                "target": selected.target,
                "operation": operation,
                "library": selected.planning.spec.library,
                "cell": selected.planning.spec.cell,
                "view": selected.planning.spec.view,
                "check": check,
                "passed": result.passed,
                **metadata,
                "physical_verification_evidence": json.loads(
                    result.evidence.canonical_json()
                ),
                "product_qualification_conclusion": False,
            },
        )
        schema = context.action.fact_schema
        if schema is None:
            raise FlowExecutionError(
                f"Action {context.action.kind!r} has no fact schema"
            )
        facts = FactSet(
            schema,
            {
                "passed": result.passed,
                "check": check,
                "evidence-role": metadata["evidence_role"],
                "evidence-level": metadata["evidence_level"],
                "evidence-scope": metadata["evidence_scope"],
                "product-qualification-conclusion": False,
            },
            FactSource(context.action.kind, context.node_id),
        )
        return AdapterResult.succeeded(
            CollectedActionResult(
                artifacts=(
                    ProducedArtifact(
                        "evidence", LAYOUT_VERIFICATION_EVIDENCE_KIND, evidence
                    ),
                ),
                facts=facts,
            )
        )


__all__ = ["ExperimentalLayoutActionAdapter"]
