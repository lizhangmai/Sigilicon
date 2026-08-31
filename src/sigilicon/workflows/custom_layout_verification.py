"""XStream plus Calibre verification for owner-planned custom layouts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from collections.abc import Callable
from typing import Any

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.physical_verification import (
    CheckedLayoutIdentity,
    CheckedSourceIdentity,
    DrcEvidence,
    PhysicalVerificationEvidence,
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
    XSTREAM_CALIBRE_LAYOUT_ADAPTER,
)
from sigilicon.virtuoso.xstream import (
    XStreamExportRequest,
    run_xstream_export,
)
from sigilicon.layout.ir import LayoutPlan
from sigilicon.layout.spec import LayoutSpec
from sigilicon.virtuoso.layout_generation import validate_layout_plan
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.layout_generation import LayoutPlanningResult
from sigilicon.workflows.layout_verification import (
    drc_evidence_from_summary,
    lvs_evidence_from_report,
    run_calibre_verification,
    find_calibre,
)
from sigilicon.workflows.layout_flow import LayoutActionAdapter, LayoutInvocation
from sigilicon.workflows.run_artifacts import RunArtifacts


@dataclass(frozen=True)
class LayoutVerificationResult:
    passed: bool
    evidence: PhysicalVerificationEvidence


def find_xstream(explicit: Path | None = None) -> Path:
    home = os.environ.get("CDSHOME")
    candidates = [
        Path(home) / "tools" / "dfII" / "bin" / "strmout"
    ] if home else []
    values = ([explicit] if explicit is not None else []) + candidates
    for value in values:
        absolute = Path(os.path.abspath(value))
        if absolute.is_file() and os.access(absolute, os.X_OK):
            return absolute
    raise FileNotFoundError(
        "strmout was not found; configure it in the PDK layout table"
    )


def _run_xstream(
    record: RunArtifacts,
    spec: LayoutSpec,
    *,
    xstream: Path,
    timeout: int,
) -> Path:
    staged_layermap = record.copy_file(
        "inputs", ("layermap",), spec.layout_pdk.layermap
    )
    cds_lib = spec.project.workspace_root / "cds.lib"
    if not cds_lib.is_file():
        raise FileNotFoundError(f"workspace cds.lib does not exist: {cds_lib}")
    work = record.directory("work")
    exported = run_xstream_export(
        XStreamExportRequest(
            executable=xstream,
            library=spec.library,
            cell=spec.cell,
            view=spec.view,
            technology_library=spec.pdk.oa.technology_library,
            layer_map=staged_layermap,
            cds_lib=cds_lib,
            work_root=work,
            timeout_seconds=timeout,
            flatten_pcells=spec.layout_pdk.xstream_flatten_pcells,
            suppressed_warnings=spec.layout_pdk.xstream_suppressed_warnings,
        )
    )
    record.write_json(
        "inputs",
        ("xstream-command.json",),
        {
            "argv": list(exported.command),
            "cwd": str(work),
            "timeout_seconds": timeout,
        },
    )
    record.write_text("logs", ("xstream-stdout.log",), exported.stdout)
    for path, name in (
        (exported.native_log_path, "strmout.log"),
        (exported.summary_path, "strmout.sum"),
    ):
        record.copy_file("logs", (name,), path)
    staged_gds = record.copy_file(
        "inputs", ("layout.gds",), exported.gds_path
    )
    staged_gds.chmod(0o444)
    return staged_gds


def run_layout_verification(
    planning: LayoutPlanningResult,
    client: Any,
    *,
    check: str,
    artifacts: RunArtifacts,
    xstream_timeout: int = 120,
    calibre_timeout: int = 600,
    xstream: Path | None = None,
    calibre: Path | None = None,
    operation_id: str | None = None,
    bind_operation: Any | None = None,
) -> LayoutVerificationResult:
    """Verify one planned layout through managed XStream and Calibre."""

    if check not in {"drc", "lvs"}:
        raise ValueError("layout verification check must be drc or lvs")
    layout_spec = planning.spec
    plan = planning.plan
    if (
        layout_spec.physical_verification is None
        or layout_spec.oa_assembly_manifest is None
    ):
        raise ValueError(
            "physical verification requires an owner policy selected by the OA assembly"
        )
    if plan.stage != "routed":
        raise ValueError("physical verification requires a routed layout plan")
    run = artifacts
    run.write_text("inputs", ("layout-plan.json",), plan.canonical_json())
    xstream_executable = find_xstream(
        xstream or layout_spec.layout_pdk.xstream_bin
    )
    calibre_executable = find_calibre(calibre or layout_spec.layout_pdk.calibre_bin)
    outcome: dict[str, object] | None = None
    typed_evidence: PhysicalVerificationEvidence | None = None
    evidence_path: Path | None = None
    deferred = None
    if not callable(bind_operation):
        raise RuntimeError("managed layout verification requires operation binding")
    with (
        workspace_operation(
            client,
            layout_spec.project.workspace_root,
            f"verify-layout-{check}",
            policy=OperationPolicy.READ_ONLY,
            operation_id=operation_id,
        ) as operation,
        operation.view_lease(
            layout_spec.library,
            cells=(layout_spec.cell,),
            views=((layout_spec.cell, layout_spec.view),),
        ),
    ):
        bind_operation(operation)
        operation.require_project_library_target(client, layout_spec.library)
        info = client.library.get(layout_spec.library, timeout=30)
        if str(info.technology_library or "") != layout_spec.pdk.oa.technology_library:
            raise RuntimeError(
                f"library {layout_spec.library} uses technology "
                f"{info.technology_library!r}, expected "
                f"{layout_spec.pdk.oa.technology_library!r}"
            )
        validate_layout_plan(client, plan, operation=operation, timeout=60)
        gds = _run_xstream(
            run,
            layout_spec,
            xstream=xstream_executable,
            timeout=xstream_timeout,
        )
        outcome = run_calibre_verification(
            run,
            layout_spec,
            plan,
            check,
            calibre=calibre_executable,
            gds=gds,
            timeout=calibre_timeout,
        )
        layout_identity = CheckedLayoutIdentity(
            artifact_identity=f"{run.run_id}:layout:gdsii",
            plan_identity=f"{layout_spec.library}:{layout_spec.cell}:layout-plan",
            result_identity=None,
            owner=layout_spec.library,
            name=layout_spec.cell,
        )
        if check == "drc":
            typed_evidence = drc_evidence_from_summary(
                read_nofollow_text(run.path("outputs", "drc-summary.rep")),
                layout=layout_identity,
                backend="cadence.xstream+calibre",
                exit_code=0,
                configuration_warnings=(
                    layout_spec.physical_verification.drc_configuration_warnings
                ),
                waiver_layers=layout_spec.physical_verification.drc_waiver_layers,
            )
        else:
            typed_evidence = lvs_evidence_from_report(
                read_nofollow_text(run.path("outputs", "lvs-report")),
                primary=layout_spec.cell,
                layout=layout_identity,
                source=CheckedSourceIdentity(
                    artifact_identity=(
                        f"{layout_spec.library}:{layout_spec.cell}:source"
                    ),
                    owner=layout_spec.library,
                    name=layout_spec.cell,
                ),
                backend="cadence.xstream+calibre",
                exit_code=0,
            )
        run.add_file("work", run.directory("work"))

        def commit() -> Path:
            nonlocal evidence_path
            assert outcome is not None
            assert typed_evidence is not None
            run.write_json(
                "outputs",
                ("completion.json",),
                {
                    "library": layout_spec.library,
                    "cell": layout_spec.cell,
                    "view": layout_spec.view,
                    "check": check,
                    "oa_content_confirmed": True,
                    **outcome,
                },
            )
            evidence_path = run.write_text(
                "outputs", ("typed-evidence.json",), typed_evidence.canonical_json()
            )
            return evidence_path

        deferred = operation.defer_commit(commit)
    if (
        deferred is None
        or not deferred.completed
        or outcome is None
        or typed_evidence is None
        or evidence_path is None
    ):
        raise RuntimeError("layout verification completed without committing its artifact")
    return LayoutVerificationResult(
        passed=bool(outcome["passed"]),
        evidence=typed_evidence,
    )


class XStreamCalibreLayoutAdapter(LayoutActionAdapter):
    """Execute one owner-planned custom-layout verification operation."""

    def _verify(
        self,
        context: ActionContext,
        selected: LayoutInvocation,
        artifacts: RunArtifacts,
    ) -> AdapterResult:
        if set(context.adapter_config) != {
            "xstream_timeout_seconds",
            "calibre_timeout_seconds",
        }:
            raise FlowExecutionError("layout verification Adapter configuration drift")
        check = self._text(context, "check")
        operation = selected.operation
        xstream = context.capabilities["tool.cadence-xstream"].executable
        calibre = context.capabilities["tool.calibre"].executable
        if xstream is None or calibre is None:
            raise FlowExecutionError(
                "layout verification tool capabilities require executable paths"
            )
        result = run_layout_verification(
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


def create_xstream_calibre_layout_adapter(
    *,
    client_factory: Callable[[], Any] | None = None,
) -> XStreamCalibreLayoutAdapter:
    """Create the Adapter while keeping Virtuoso client selection at this seam."""

    if client_factory is None:
        from sigilicon.virtuoso.client import get_client

        client_factory = get_client
    return XStreamCalibreLayoutAdapter(client_factory=client_factory)


__all__ = [
    "create_xstream_calibre_layout_adapter",
    "LayoutVerificationResult",
    "XSTREAM_CALIBRE_LAYOUT_ADAPTER",
    "XStreamCalibreLayoutAdapter",
    "find_xstream",
    "run_layout_verification",
]
