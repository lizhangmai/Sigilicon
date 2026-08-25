"""Automated execution of the same canonical Maestro view used manually."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any
import uuid

from sigilicon.artifacts import ArtifactRecord, new_identity
from sigilicon.domain.oa_simulation import oa_simulation_fingerprints
from sigilicon.domain.provenance import digest
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.attestation import attest_native_setup
from sigilicon.virtuoso.maestro_batch import run_isolated_maestro
from sigilicon.virtuoso.maestro_rdb import (
    read_native_maestro_rdb_export,
    reconstruct_native_diagnostic,
)
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation
from sigilicon.workflows.oa_library import (
    OALibraryRebuildPlan,
    TestbenchRebuildStep,
    check_oa_parity,
    plan_oa_library_rebuild,
)


@dataclass(frozen=True)
class OAMaestroRunResult:
    """Completed managed OA Maestro run and its persistent result identity."""

    run_id: str
    run_dir: Path
    manifest_path: Path
    library: str
    testbench: str
    history: str
    source_fingerprint: str
    oa_materialization_fingerprint: str
    elaborated_netlist_fingerprint: str
    result_database_export: Path
    normalized_result_database: Path
    run_summary: Path
    scalar_output_count: int
    semantic_fingerprint: str = ""


def _elaborated_netlist_fingerprint(work_dir: Path) -> str:
    """Hash the unique final Cadence netlist content from one Maestro run."""

    ams_netlists = tuple(
        path for path in work_dir.rglob("netlist.vams") if path.is_file()
    )
    # An AMS run emits both the elaborated ``netlist.vams`` and a protected
    # file named ``netlist`` that describes the config-view binding.  The
    # latter is not a second elaborated design and must not participate in the
    # identity comparison.  Current pure-Spectre Maestro runs emit their
    # elaborated design as ``netlist/spectre.inp``.
    netlists = ams_netlists
    if not netlists:
        netlists = tuple(
            path
            for path in work_dir.rglob("spectre.inp")
            if path.is_file()
            and path.parent.name == "netlist"
        )
    if not netlists:
        raise RuntimeError(
            "Maestro run did not produce an elaborated AMS or Spectre netlist"
        )
    fingerprints = {
        hashlib.sha256(path.read_bytes()).hexdigest(): path for path in netlists
    }
    if len(fingerprints) != 1:
        details = [
            {"path": str(path), "sha256": fingerprint}
            for fingerprint, path in sorted(fingerprints.items())
        ]
        raise RuntimeError(
            f"Maestro run produced conflicting elaborated netlist content: {details}"
        )
    return next(iter(fingerprints))


def _run_native_oa_maestro_testbench(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    timeout: int,
) -> OAMaestroRunResult:
    fingerprints = oa_simulation_fingerprints(
        step.simulation,
        step.canonical_source,
    )
    owners = {
        cell.owner for cell in plan.source.cells if cell.cell == step.cell
    }
    if len(owners) != 1:
        raise ValueError(
            f"OA testbench {step.cell} does not resolve to one source owner"
        )
    paths = ProjectContext.from_project_root(plan.source.project_root)
    record = ArtifactRecord.begin(
        paths.artifacts.execution(
            owner=next(iter(owners)),
            target=step.cell,
            flow="oa-maestro",
            variant="native-rdb",
            identity=new_identity(),
            artifact_kind="oa_maestro_simulation",
            identity_kind="run_id",
        ),
        entities={
            "library": plan.library,
            "cell": step.simulation.dut,
            "testbench": step.cell,
        },
        operation="canonical-oa-maestro-native-rdb",
        backend="virtuoso-maestro-spectre",
        source_fingerprint=fingerprints.exact,
        semantic_fingerprint=fingerprints.semantic,
    )
    try:
        result = _run_native_oa_maestro_testbench_impl(
            plan,
            step,
            client,
            timeout=timeout,
            record=record,
        )
        record.succeed(
            completion_evidence=(
                result.run_summary,
                result.normalized_result_database,
            ),
            details={
                "history": result.history,
                "oa_materialization_fingerprint": (
                    result.oa_materialization_fingerprint
                ),
                "elaborated_netlist_fingerprint": (
                    result.elaborated_netlist_fingerprint
                ),
            },
        )
        return result
    except BaseException as error:
        if record.status == "running":
            try:
                record.fail(error)
            except Exception as record_error:
                error.add_note(
                    "could not record OA Maestro artifact failure: "
                    f"{record_error}"
                )
        raise


@contextmanager
def _registered_oa_maestro_operation(
    client: Any,
    workspace_root: Path,
    record: ArtifactRecord,
) -> Iterator[Any]:
    """Bind the workspace safety lifecycle to the one managed run artifact."""

    operation = None
    try:
        with workspace_operation(
            client,
            workspace_root,
            "run-canonical-oa-maestro-native-rdb",
            policy=OperationPolicy.MAESTRO_RUN,
        ) as operation:
            operation.register_artifact(record)
            yield operation
    except BaseException as error:
        if record.status == "running":
            try:
                record.fail(
                    error,
                    uncertain_reason=(
                        operation.uncertain_reason if operation is not None else None
                    ),
                )
            except Exception as record_error:
                error.add_note(
                    "could not record OA Maestro workspace failure: "
                    f"{record_error}"
                )
        raise


def _run_native_oa_maestro_testbench_impl(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    timeout: int,
    record: ArtifactRecord,
) -> OAMaestroRunResult:
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
    oa_materialization_fingerprint = digest(oa_check)
    fingerprint = str(record.manifest["fingerprints"]["source"])
    semantic_fingerprint = str(record.manifest["fingerprints"]["semantic"])
    record.copy_file("inputs", ("simulation.toml",), spec.path)
    record.copy_file("inputs", ("setup.il",), native_setup.source)
    record.copy_file("inputs", ("native_rdb.toml",), rdb_contract.path)
    record.copy_file("inputs", ("cell.toml",), spec.path.parent / "cell.toml")
    rdb_contract_hasher = hashlib.sha256()
    for index, source in enumerate(
        (rdb_contract.path, *rdb_contract.support_sources)
    ):
        label = f"{index:02d}-{source.name}".encode("utf-8")
        payload = source.read_bytes()
        rdb_contract_hasher.update(len(label).to_bytes(8, "big"))
        rdb_contract_hasher.update(label)
        rdb_contract_hasher.update(len(payload).to_bytes(8, "big"))
        rdb_contract_hasher.update(payload)
        if index:
            record.copy_file(
                "inputs",
                ("support", f"{index:02d}-{source.name}"),
                source,
            )
    rdb_contract_fingerprint = rdb_contract_hasher.hexdigest()
    parsed_results: dict[str, Any] | None = None
    rdb_export = record.path("work", "maestro-rdb.tsv")
    paths = ProjectContext.from_project_root(plan.source.project_root)

    with _registered_oa_maestro_operation(
        client,
        paths.workspace_root,
        record,
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
        record.write_json(
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
            work_dir=record.paths.role("work"),
            worker_log=record.path("work", "virtuoso.log"),
            nonce=nonce,
            timeout=timeout,
            operation=operation,
            result_completion_probe=complete_results,
            rdb_export=rdb_export,
        )
        if parsed_results is None:
            raise RuntimeError("Maestro completed without an official RDB result")
        elaborated_netlist_fingerprint = _elaborated_netlist_fingerprint(
            record.paths.role("work")
        )
        record.write_text("logs", ("virtuoso-worker.log",), result.worker_log_text)
        result_export = record.copy_file(
            "outputs", ("maestro-rdb.tsv",), rdb_export
        )
        parsed = record.write_json("outputs", ("maestro-rdb.json",), parsed_results)
        diagnostic_equivalence = reconstruct_native_diagnostic(
            parsed_results,
            rdb_contract,
        )
        diagnostic_equivalence_path = None
        if diagnostic_equivalence is not None:
            diagnostic_equivalence_path = record.write_json(
                "outputs", ("diagnostic-equivalence.json",), diagnostic_equivalence
            )
        record.write_json("outputs", ("oa-library-check.json",), oa_check)
        record.add_file(
            "work",
            record.paths.role("work"),
            label="native Maestro work directory",
        )
        run_summary = record.write_json(
            "outputs",
            ("run-summary.json",),
            {
                "run_id": record.paths.identity,
                "library": plan.library,
                "testbench": step.cell,
                "view": "maestro",
                "history": result.history,
                "source_fingerprint": fingerprint,
                "semantic_fingerprint": semantic_fingerprint,
                "native_rdb_contract_fingerprint": rdb_contract_fingerprint,
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
                        diagnostic_equivalence_path.relative_to(record.paths.root)
                    )
                ),
                "diagnostic_kind": (
                    None
                    if rdb_contract.diagnostic_equivalence is None
                    else rdb_contract.diagnostic_equivalence.kind
                ),
                "diagnostic_scalar_count": len(rdb_contract.diagnostic_scalar_names),
                "oa_materialization_fingerprint": oa_materialization_fingerprint,
                "elaborated_netlist_fingerprint": elaborated_netlist_fingerprint,
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
                "product_qualification_conclusion": False,
            },
        )

    return OAMaestroRunResult(
        run_id=record.paths.identity,
        run_dir=record.paths.root,
        manifest_path=record.paths.manifest,
        library=plan.library,
        testbench=step.cell,
        history=result.history,
        source_fingerprint=fingerprint,
        oa_materialization_fingerprint=oa_materialization_fingerprint,
        elaborated_netlist_fingerprint=elaborated_netlist_fingerprint,
        result_database_export=result_export,
        normalized_result_database=parsed,
        run_summary=run_summary,
        scalar_output_count=int(parsed_results["expression_count"]),
        semantic_fingerprint=semantic_fingerprint,
    )


def run_oa_maestro_testbench(
    plan: OALibraryRebuildPlan,
    step: TestbenchRebuildStep,
    client: Any,
    *,
    timeout: int = 600,
) -> OAMaestroRunResult:
    """Run one source-attested schema-3 OA Maestro view through native RDB."""

    if step not in plan.testbenches:
        raise ValueError("testbench is not part of the selected OA assembly plan")
    if step.simulation.native_setup is None:
        raise ValueError(
            f"OA testbench {step.cell} is not a schema-3 native simulation contract"
        )
    return _run_native_oa_maestro_testbench(
        plan,
        step,
        client,
        timeout=timeout,
    )


def run_named_oa_maestro_testbench(
    manifest: Path,
    *,
    project_root: Path,
    library: str,
    testbench: str,
    client: Any,
    timeout: int = 600,
) -> OAMaestroRunResult:
    """Resolve and run one testbench through its source assembly contract."""

    plan = plan_oa_library_rebuild(
        manifest,
        project_root=project_root,
        library=library,
    )
    matches = [step for step in plan.testbenches if step.cell == testbench]
    if len(matches) != 1:
        raise ValueError(f"unknown OA testbench in assembly: {testbench}")
    return run_oa_maestro_testbench(
        plan,
        matches[0],
        client,
        timeout=timeout,
    )
