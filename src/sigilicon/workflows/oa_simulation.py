"""Automated execution of the same canonical Maestro view used manually."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
import uuid

from sigilicon.domain.native_diagnostics import NativeDiagnosticReport
from sigilicon.domain.netlist import NetlistSnapshot
from sigilicon.domain.source import TextSourceSnapshot
from sigilicon.virtuoso.attestation import attest_native_setup
from sigilicon.virtuoso.maestro_batch import run_isolated_maestro
from sigilicon.virtuoso.maestro_rdb import read_native_maestro_rdb_export
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    TestbenchRebuildStep,
    check_oa_parity,
)
from sigilicon.workflows.run_artifacts import RunArtifacts


@dataclass(frozen=True)
class OAMaestroEvidence:
    """Evaluated evidence from one completed native Maestro result."""

    status: Literal["pass", "fail", "not_evaluated", "inconclusive"]
    sources: tuple[str, ...]
    overall_spec_status: object
    per_output_spec_status: tuple[str, ...]
    diagnostic_passed: bool | None

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "passed": self.passed,
            "sources": list(self.sources),
            "overall_spec_status": self.overall_spec_status,
            "per_output_spec_status": list(self.per_output_spec_status),
            "diagnostic_passed": self.diagnostic_passed,
        }


def _spec_status(value: object) -> Literal[
    "pass", "fail", "not_evaluated", "inconclusive"
]:
    if value is None:
        return "not_evaluated"
    if isinstance(value, bool):
        return "pass" if value else "fail"
    token = str(value).strip().lower()
    compact = "".join(token.replace('"', "").split())
    if compact in {"t", "true", "pass", "passed", "((overallt))"}:
        return "pass"
    if compact in {"false", "fail", "failed", "((overallnil))"}:
        return "fail"
    if compact in {"", "nil", "none", "undefined", "not_evaluated"}:
        return "not_evaluated"
    return "inconclusive"


def evaluate_oa_maestro_evidence(
    *,
    overall_spec_status: object,
    per_output_spec_status: tuple[str, ...],
    diagnostic_report: NativeDiagnosticReport | None,
) -> OAMaestroEvidence:
    """Evaluate official Maestro and owner diagnostic results without guessing."""

    overall = _spec_status(overall_spec_status)
    outputs = tuple(_spec_status(value) for value in per_output_spec_status)
    diagnostic_passed = (
        None if diagnostic_report is None else diagnostic_report.passed
    )

    failed_sources: list[str] = []
    if overall == "fail":
        failed_sources.append("maestro-overall")
    if "fail" in outputs:
        failed_sources.append("maestro-outputs")
    if diagnostic_passed is False:
        failed_sources.append("native-diagnostic")
    if failed_sources:
        status = "fail"
        sources = tuple(failed_sources)
    elif diagnostic_passed is True:
        status = "pass"
        sources = ("native-diagnostic",)
    elif outputs and all(value == "pass" for value in outputs):
        status = "pass"
        sources = ("maestro-outputs",)
    elif overall == "inconclusive":
        status = "inconclusive"
        sources = ("maestro-overall",)
    elif "inconclusive" in outputs or "pass" in outputs:
        status = "inconclusive"
        sources = ("maestro-outputs",)
    else:
        status = "not_evaluated"
        sources = ()

    return OAMaestroEvidence(
        status=status,
        sources=sources,
        overall_spec_status=overall_spec_status,
        per_output_spec_status=per_output_spec_status,
        diagnostic_passed=diagnostic_passed,
    )


@dataclass(frozen=True)
class OAMaestroExecutionResult:
    """Maestro result written into an execution lifecycle owned by the caller."""

    library: str
    testbench: str
    history: str
    elaborated_netlist: Path
    result_database_export: Path
    normalized_result_database: Path
    run_summary: Path
    scalar_output_count: int
    evidence: OAMaestroEvidence

    @property
    def passed(self) -> bool:
        return self.evidence.passed

    def as_dict(self) -> dict[str, object]:
        """Return domain evidence without inventing another run identity."""

        return {
            "passed": self.passed,
            "execution": "oa-maestro",
            "execution_status": "completed",
            "evidence_status": self.evidence.status,
            "evidence": self.evidence.as_dict(),
            "library": self.library,
            "testbench": self.testbench,
            "history": self.history,
            "elaborated_netlist": str(self.elaborated_netlist),
            "result_database_export": str(self.result_database_export),
            "normalized_result_database": str(self.normalized_result_database),
            "run_summary": str(self.run_summary),
            "scalar_output_count": self.scalar_output_count,
            "product_qualification_conclusion": False,
        }


def _elaborated_netlist(work_dir: Path, history: str) -> Path:
    """Return the final netlist from the completed Maestro history."""

    ams_netlists = tuple(
        path for path in work_dir.rglob("netlist.vams") if path.is_file()
    )
    # An AMS run emits both the elaborated ``netlist.vams`` and a protected
    # file named ``netlist`` that describes the config-view binding.  The
    # latter is not a second elaborated design and must not participate in the
    # selection. Pure-Spectre Maestro runs emit their complete simulator deck
    # as ``netlist/input.scs``; ``spectre.inp`` is only a short source-statement
    # marker and is not the elaborated design.
    candidates = ams_netlists
    if not candidates:
        candidates = tuple(
            path
            for path in work_dir.rglob("input.scs")
            if path.is_file()
            and path.parent.name == "netlist"
        )
    if not candidates:
        raise RuntimeError(
            "Maestro run did not produce an elaborated AMS or Spectre netlist"
        )
    netlists = tuple(
        path
        for path in candidates
        if history in path.parts
        and "psf" not in path.parts
        and not any(part.startswith(".tmpADEDir") for part in path.parts)
    )
    if len(netlists) != 1:
        raise RuntimeError(
            "Maestro run did not resolve one final netlist for history "
            f"{history}: {[str(path) for path in candidates]}"
        )
    return netlists[0]


@contextmanager
def _registered_oa_maestro_operation(
    client: Any,
    workspace_root: Path,
    *,
    operation_id: str,
    bind_operation: Callable[[Any], None],
    record_uncertainty: Callable[[str], None] | None = None,
) -> Iterator[Any]:
    """Bind the workspace safety lifecycle to its caller-owned run record."""

    operation = None
    try:
        with workspace_operation(
            client,
            workspace_root,
            "run-canonical-oa-maestro-native-rdb",
            policy=OperationPolicy.MAESTRO_RUN,
            operation_id=operation_id,
        ) as operation:
            bind_operation(operation)
            yield operation
    except BaseException:
        reason = getattr(operation, "uncertain_reason", None)
        if isinstance(reason, str) and reason and record_uncertainty is not None:
            record_uncertainty(reason)
        raise


def _run_native_oa_maestro_testbench_impl(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    timeout: int,
    artifacts: RunArtifacts,
    operation_id: str,
    bind_operation: Callable[[Any], None],
    record_uncertainty: Callable[[str], None] | None = None,
) -> OAMaestroExecutionResult:
    """Run one source-owned setup and consume Cadence's read-only RDB API."""

    spec = step.simulation
    native_setup = spec.native_setup
    if native_setup is None:
        raise ValueError("native Maestro runner requires a schema-3 simulation spec")
    rdb_contract = native_setup.rdb_contract
    if rdb_contract is None:
        raise ValueError(
            "native Maestro runner requires the source-owned native_rdb.toml "
            f"identity contract for {step.cell}"
        )

    oa_check = check_oa_parity(
        plan,
        client,
        testbench=step.cell,
        timeout=min(timeout, 300),
        acquire_flow_lock=False,
        record_incident=False,
    )
    if oa_check.get("passed") is not True:
        raise RuntimeError(
            f"OA testbench parity check failed: {oa_check}"
        )
    _record_native_oa_maestro_inputs(artifacts, step)
    parsed_results: dict[str, Any] | None = None
    rdb_export = artifacts.path("work", "maestro-rdb.tsv")
    project = plan.source.project

    with _registered_oa_maestro_operation(
        client,
        project.workspace_root,
        operation_id=operation_id,
        bind_operation=bind_operation,
        record_uncertainty=record_uncertainty,
    ) as operation, operation.view_lease(
        plan.library,
        cells=(step.cell,),
        views=((step.cell, "config"), (step.cell, "maestro")),
    ):
        setup_attestation = attest_native_setup(
            spec,
            client,
            operation=operation,
            timeout=min(timeout, 300),
        )
        artifacts.write_json(
            "outputs",
            ("setup-semantic-attestation.json",),
            setup_attestation,
        )
        nonce = uuid.uuid4().hex

        def complete_results(_history: str) -> bool:
            nonlocal parsed_results
            parsed_results = read_native_maestro_rdb_export(
                rdb_export,
                expected_point_count=rdb_contract.point_count,
                expected_corners=rdb_contract.corners,
                expected_tests=rdb_contract.tests,
                expected_outputs=rdb_contract.scalar_names,
                expected_expression_count=rdb_contract.expected_expression_count,
                nullable_outputs=rdb_contract.nullable_scalar_names,
            )
            return True

        result = run_isolated_maestro(
            client,
            library=plan.library,
            cell=step.cell,
            variables={},
            work_dir=artifacts.directory("work"),
            worker_log=artifacts.path("work", "virtuoso.log"),
            nonce=nonce,
            timeout=timeout,
            operation=operation,
            result_completion_probe=complete_results,
            rdb_export=rdb_export,
        )
        if parsed_results is None:
            raise RuntimeError("Maestro completed without an official RDB result")
        final_netlist = _elaborated_netlist(
            artifacts.directory("work"), result.history
        )
        elaborated_netlist = artifacts.copy_file(
            "outputs",
            (
                "elaborated-netlist.vams"
                if final_netlist.name == "netlist.vams"
                else "elaborated-netlist.scs",
            ),
            final_netlist,
            label="final Cadence elaborated netlist",
        )
        artifacts.write_text("logs", ("virtuoso-worker.log",), result.worker_log_text)
        result_export = artifacts.copy_file(
            "outputs", ("maestro-rdb.tsv",), rdb_export
        )
        parsed = artifacts.write_json("outputs", ("maestro-rdb.json",), parsed_results)
        diagnostic_report = rdb_contract.reconstruct_diagnostic(parsed_results)
        diagnostic_equivalence_path = None
        if diagnostic_report is not None:
            diagnostic_equivalence_path = artifacts.write_json(
                "outputs",
                ("diagnostic-equivalence.json",),
                diagnostic_report.as_dict(),
            )
        artifacts.write_json("outputs", ("oa-library-check.json",), oa_check)
        artifacts.add_file(
            "work",
            artifacts.directory("work"),
            label="native Maestro work directory",
        )
        per_output_spec_status = tuple(
            str(output["spec_status"]) for output in parsed_results["outputs"]
        )
        evidence = evaluate_oa_maestro_evidence(
            overall_spec_status=parsed_results["overall_spec_status"],
            per_output_spec_status=per_output_spec_status,
            diagnostic_report=diagnostic_report,
        )
        run_summary = artifacts.write_json(
            "outputs",
            ("run-summary.json",),
            {
                "run_id": artifacts.run_id,
                "library": plan.library,
                "testbench": step.cell,
                "view": "maestro",
                "history": result.history,
                "source": dict(artifacts.source),
                "expected_rdb_identity": {
                    "point_count": rdb_contract.point_count,
                    "corners": list(rdb_contract.corners),
                    "tests": list(rdb_contract.tests),
                    "outputs": list(rdb_contract.scalar_names),
                    "expression_count": rdb_contract.expected_expression_count,
                },
                "waveform_output_count": len(rdb_contract.waveform_outputs),
                "diagnostic_equivalence": (
                    None
                    if diagnostic_equivalence_path is None
                    else str(
                        diagnostic_equivalence_path.relative_to(artifacts.root)
                    )
                ),
                "diagnostic_kind": (
                    None
                    if rdb_contract.diagnostic_equivalence is None
                    else rdb_contract.diagnostic_equivalence.kind
                ),
                "diagnostic_scalar_count": len(rdb_contract.diagnostic_scalar_names),
                "elaborated_netlist": str(
                    elaborated_netlist.relative_to(artifacts.root)
                ),
                "result_source": "Cadence maeReadResDB/point->outputs",
                "point_count": parsed_results["point_count"],
                "expression_count": parsed_results["expression_count"],
                "scalar_output_count": parsed_results["scalar_output_count"],
                "scalar_values_finite": parsed_results["scalar_values_finite"],
                "nullable_output_count": parsed_results["nullable_output_count"],
                "per_output_spec_status": [
                    {
                        "corner": output["corner"],
                        "test": output["test"],
                        "name": output["name"],
                        "status": output["spec_status"],
                    }
                    for output in parsed_results["outputs"]
                ],
                "rdb_identity": parsed_results["identity"],
                "overall_spec_status": parsed_results["overall_spec_status"],
                "simulation_completed": True,
                "execution_status": "completed",
                "evidence_status": evidence.status,
                "evidence": evidence.as_dict(),
                "passed": evidence.passed,
                "product_qualification_conclusion": False,
            },
        )

    return OAMaestroExecutionResult(
        library=plan.library,
        testbench=step.cell,
        history=result.history,
        elaborated_netlist=elaborated_netlist,
        result_database_export=result_export,
        normalized_result_database=parsed,
        run_summary=run_summary,
        scalar_output_count=int(parsed_results["expression_count"]),
        evidence=evidence,
    )


def _record_native_oa_maestro_inputs(
    record: RunArtifacts,
    step: TestbenchRebuildStep,
) -> None:
    """Persist the exact plan-owned sources consumed by one Maestro run."""

    _validate_native_oa_maestro_inputs(step)
    spec = step.simulation
    native_setup = spec.native_setup
    rdb_contract = native_setup.rdb_contract
    assert isinstance(spec.source_snapshot, TextSourceSnapshot)
    assert rdb_contract is not None
    assert isinstance(rdb_contract.source_snapshot, TextSourceSnapshot)
    support_snapshots = rdb_contract.support_source_snapshots
    if tuple(source.source_path for source in support_snapshots) != (
        rdb_contract.support_sources
    ):
        raise ValueError("native Maestro support source snapshot identity drift")
    record.write_text(
        "inputs",
        ("simulation.toml",),
        spec.source_snapshot.text,
        label="exact native simulation contract",
    )
    record.write_text(
        "inputs",
        ("setup.il",),
        native_setup.source_snapshot.text,
        label="exact native ADE/Maestro setup",
    )
    record.write_text(
        "inputs",
        ("native_rdb.toml",),
        rdb_contract.source_snapshot.text,
        label="exact native RDB identity contract",
    )
    record.write_text(
        "inputs",
        ("testbench.scs",),
        step.source_snapshot.text,
        label="exact canonical testbench netlist",
    )
    for index, source in enumerate(support_snapshots, start=1):
        record.write_text(
            "inputs",
            ("support", f"{index:02d}-{source.source_path.name}"),
            source.text,
            label="exact native diagnostic support source",
        )


def _validate_native_oa_maestro_inputs(step: TestbenchRebuildStep) -> None:
    """Require a complete, identity-bound source inventory before OA access."""

    spec = step.simulation
    native_setup = spec.native_setup
    rdb_contract = native_setup.rdb_contract
    if not isinstance(spec.source_snapshot, TextSourceSnapshot):
        raise ValueError("native Maestro plan has no simulation source snapshot")
    if spec.source_snapshot.source_path != spec.path:
        raise ValueError("native Maestro simulation source snapshot identity drift")
    if not isinstance(native_setup.source_snapshot, TextSourceSnapshot):
        raise ValueError("native Maestro plan has no setup source snapshot")
    if native_setup.source_snapshot.source_path != native_setup.source:
        raise ValueError("native Maestro setup source snapshot identity drift")
    if rdb_contract is None:
        raise ValueError("native Maestro plan has no RDB contract")
    if not isinstance(rdb_contract.source_snapshot, TextSourceSnapshot):
        raise ValueError("native Maestro plan has no RDB source snapshot")
    if rdb_contract.source_snapshot.source_path != rdb_contract.path:
        raise ValueError("native Maestro RDB source snapshot identity drift")
    if not isinstance(step.source_snapshot, NetlistSnapshot):
        raise ValueError("native Maestro plan has no testbench netlist snapshot")
    if step.source_snapshot.source_path != step.canonical_source:
        raise ValueError("native Maestro testbench snapshot identity drift")
    support_snapshots = rdb_contract.support_source_snapshots
    if any(
        not isinstance(source, TextSourceSnapshot)
        for source in support_snapshots
    ) or tuple(source.source_path for source in support_snapshots) != (
        rdb_contract.support_sources
    ):
        raise ValueError("native Maestro support source snapshot identity drift")


def execute_oa_maestro_testbench(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    artifacts: RunArtifacts,
    operation_id: str,
    bind_operation: Callable[[Any], None],
    record_uncertainty: Callable[[str], None] | None = None,
    timeout: int = 600,
) -> OAMaestroExecutionResult:
    """Execute one resolved Maestro view inside a caller-owned run lifecycle."""

    if step not in plan.testbenches:
        raise ValueError("testbench is not part of the selected OA assembly plan")
    if step.simulation.native_setup is None:
        raise ValueError(
            f"OA testbench {step.cell} is not a schema-3 native simulation contract"
        )
    _validate_native_oa_maestro_inputs(step)
    return _run_native_oa_maestro_testbench_impl(
        plan,
        step,
        client,
        timeout=timeout,
        artifacts=artifacts,
        operation_id=operation_id,
        bind_operation=bind_operation,
        record_uncertainty=record_uncertainty,
    )
