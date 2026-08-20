"""Legacy: run a generated ADE AMS test and enforce its truth-table contract."""

from __future__ import annotations

import stat
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import uuid

from sigilicon.ams.provenance import (
    ams_fingerprint,
    ams_run_fingerprint,
    normalize_run_variables,
)
from sigilicon.ams.load_contract import validate_load_attestation
from sigilicon.artifacts import ArtifactRecord, new_identity, read_nofollow_text
from sigilicon.ams.results import parse_results, render_truth_table, validate_truth_contract
from sigilicon.ams.artifacts import load_committed_ade_setup
from sigilicon.ams.spec import AmsSpec
from sigilicon.paths import ProjectContext, validate_artifact_component
from sigilicon.domain.provenance import design_fingerprint
from sigilicon.virtuoso.oa import (
    validate_cell_fingerprint,
)
from sigilicon.virtuoso.legacy_ade import (
    read_oa_load_instances,
    validate_ade_component_views,
)
from sigilicon.virtuoso.maestro_batch import (
    IsolatedMaestroRunResult,
    run_isolated_maestro,
)
from sigilicon.virtuoso.workspace import OperationPolicy, workspace_operation


@dataclass(frozen=True)
class AdeRunResult:
    namespace_dir: Path
    setup_dir: Path
    run_dir: Path
    history: str
    simulator_status: str
    source_log: Path
    copied_log: Path
    truth_table: Path
    vectors: int
    run_fingerprint: str


_XRUN_EXIT_RE = re.compile(r"(?m)^TOOL:\s+xrun(?:\(64\))?.*\bExiting on\b")
_SPECTRE_SUCCESS_RE = re.compile(
    r"(?im)^\s*spectre completes with 0 errors(?:,|\s|$)"
)
_SIMULATOR_FATAL_RE = re.compile(
    r"(?im)(?:^|\n)\s*(?:xrun(?:\(64\))?|xmelab|xmsim):\s*\*[EF],"
    r"|(?:^|\n)\s*(?:fatal|error):"
    r"|(?:^|\n)\s*spectre completes with [1-9][0-9]* errors(?:,|\s|$)"
)


def find_ade_xrun_log(workdir: Path, spec: AmsSpec, history: str) -> Path:
    validate_artifact_component(history, "Maestro history")
    history_root = (
        workdir
        / "simulation"
        / spec.design.library
        / spec.testbench
        / "maestro"
        / "results"
        / "maestro"
        / history
    )
    candidates: list[Path] = []
    for path in history_root.rglob("xrun.log"):
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"no xrun.log found under {history_root}")
    concrete: list[tuple[Path, str, str]] = []
    aggregate: list[tuple[Path, str]] = []
    for path in candidates:
        relative = path.relative_to(history_root)
        parts = relative.parts
        if (
            len(parts) == 4
            and parts[0].isdigit()
            and str(int(parts[0])) == parts[0]
            and int(parts[0]) > 0
            and parts[2:] == ("psf", "xrun.log")
        ):
            point_file = path.parent.parent.joinpath("netlist", "pointID.txt")
            try:
                point = read_nofollow_text(point_file).strip()
            except (OSError, RuntimeError, UnicodeDecodeError):
                continue
            if point == parts[0]:
                concrete.append((path, parts[0], parts[1]))
        elif (
            len(parts) == 4
            and parts[0] == "psf"
            and parts[2:] == ("psf", "xrun.log")
        ):
            aggregate.append((path, parts[1]))

    if len(candidates) == 1 and (concrete or aggregate):
        return candidates[0]
    if (
        len(candidates) == 2
        and len(concrete) == 1
        and len(aggregate) == 1
    ):
        concrete_path, _point, test = concrete[0]
        _aggregate_path, aggregate_test = aggregate[0]
        # The concrete point is already bound to its own pointID.txt.  The
        # aggregate xrun.log is a duplicate history record; selecting the
        # unique concrete point needs no aggregate run-object parsing.
        if aggregate_test == test:
            return concrete_path
    raise RuntimeError(
        f"ambiguous or unsupported xrun.log provenance for Maestro history "
        f"{history!r}: found {len(candidates)} candidates under {history_root}"
    )


def confirmed_ade_simulator_result(
    workdir: Path,
    spec: AmsSpec,
    history: str,
) -> bool:
    """Confirm Xrun exited; functional success is validated after cleanup."""

    source_log = find_ade_xrun_log(workdir, spec, history)
    text = read_nofollow_text(source_log, errors="replace")
    return _XRUN_EXIT_RE.search(text) is not None


def validate_ade_simulator_success(text: str) -> None:
    """Require an explicit clean Spectre summary and Xrun terminal record."""

    if _XRUN_EXIT_RE.search(text) is None:
        raise RuntimeError("ADE Xrun log has no terminal exit record")
    if _SPECTRE_SUCCESS_RE.search(text) is None:
        raise RuntimeError("ADE simulator log has no zero-error Spectre completion")
    if _SIMULATOR_FATAL_RE.search(text) is not None:
        raise RuntimeError("ADE simulator log contains a fatal or error result")


def run_ade(
    spec: AmsSpec,
    client: Any,
    *,
    artifact_root: Path | None = None,
    variables: Mapping[str, str] | None = None,
    timeout: int = 600,
) -> AdeRunResult:
    """Execute the Maestro test, archive Xrun output, and fail on truth mismatch."""

    design = spec.design
    normalized_variables = normalize_run_variables(variables)
    setup_fingerprint = ams_fingerprint(spec)
    run_fingerprint = ams_run_fingerprint(
        spec,
        backend="ade",
        variables=normalized_variables,
    )
    paths = ProjectContext.from_project_root(
        design.project_root,
        artifact_root=artifact_root,
    )
    namespace = paths.artifacts.ade(design.library, spec.testbench)
    committed_setup = load_committed_ade_setup(
        namespace,
        setup_fingerprint,
    )
    attempt = ArtifactRecord.begin(
        namespace.run(new_identity()),
        entities={
            "library": design.library,
            "cell": design.cell,
            "testbench": spec.testbench,
        },
        operation="simulate",
        backend="ade",
        source_fingerprint=design_fingerprint(design),
        setup_fingerprint=setup_fingerprint,
        run_fingerprint=run_fingerprint,
        reference_links={
            "setup_manifest": committed_setup.manifest_path.relative_to(
                paths.artifact_root
            ).as_posix()
        },
    )
    result: AdeRunResult | None = None
    operation: Any | None = None
    run: IsolatedMaestroRunResult | None = None
    truth_table_for_commit: Path | None = None
    commit_details: dict[str, Any] = {"variables": normalized_variables}
    body_failure: BaseException | None = None

    def commit_run() -> Path:
        if truth_table_for_commit is None:
            raise RuntimeError("ADE run has no verified truth table to commit")
        return attempt.succeed(
            completion_evidence=(truth_table_for_commit,),
            details=commit_details,
        )

    def record_aborted(error: BaseException) -> None:
        if attempt.status != "running":
            return
        for name, label in (
            ("maestro-worker.il", "isolated Maestro worker control"),
            ("maestro-worker.cds.lib", "canonical worker cds.lib"),
        ):
            evidence = attempt.path("work", name)
            if evidence.is_file():
                attempt.add_file("work", evidence, label=label)
        attempt.fail(
            body_failure or error,
            uncertain_reason=getattr(operation, "uncertain_reason", None),
            details=commit_details,
        )

    with (
        attempt.failure_boundary(
            uncertainty=lambda: getattr(operation, "uncertain_reason", None),
            details=lambda: commit_details,
        ),
        workspace_operation(
            client,
            paths.workspace_root,
            "run-ams-ade",
            policy=OperationPolicy.MAESTRO_RUN,
        ) as operation,
    ):
        operation.register_artifact(attempt)
        operation.defer_commit(commit_run, on_failure=record_aborted)
        with operation.view_lease(design.library):
            validate_cell_fingerprint(
                client,
                design.library,
                design.cell,
                design_fingerprint(design),
                operation=operation,
            )
            validate_ade_component_views(
                client,
                paths.workspace_root,
                design.library,
                spec.testbench,
                spec.wrapper_cell,
                committed_setup.components,
            )
            actual_load = validate_load_attestation(
                design.outputs,
                spec.simulation.interface.load_cap,
                read_oa_load_instances(
                    client,
                    design.library,
                    spec.wrapper_cell,
                    operation=operation,
                ),
            )
            if (
                committed_setup.components["load"].get("oa_load_attestation")
                != actual_load
            ):
                raise RuntimeError(
                    "ADE OA load semantics no longer match the committed setup; "
                    "rerun setup-ams-ade"
                )
            try:
                worker_nonce = uuid.uuid4().hex
                attempt.add_file(
                    "work",
                    attempt.paths.role("work"),
                    label="isolated Maestro and Xcelium native work directory",
                )
                run = run_isolated_maestro(
                    client,
                    library=design.library,
                    cell=spec.testbench,
                    variables=normalized_variables,
                    work_dir=attempt.paths.role("work"),
                    worker_log=attempt.path("work", "virtuoso.log"),
                    nonce=worker_nonce,
                    timeout=timeout,
                    operation=operation,
                    result_completion_probe=lambda history: (
                        confirmed_ade_simulator_result(
                            attempt.paths.role("work"),
                            spec,
                            history,
                        )
                    ),
                )
                attempt.add_file(
                    "work",
                    attempt.path("work", "maestro-worker.il"),
                    label="isolated Maestro worker control",
                )
                attempt.add_file(
                    "work",
                    attempt.path("work", "maestro-worker.cds.lib"),
                    label="canonical worker cds.lib",
                )
                attempt.write_text(
                    "logs",
                    ("virtuoso-worker.log",),
                    run.worker_log_text,
                    label="isolated Virtuoso worker log",
                )
                attempt.write_text(
                    "logs",
                    ("virtuoso-worker.stdout.log",),
                    run.stdout,
                    label="isolated Virtuoso worker stdout",
                )
                source_log = find_ade_xrun_log(
                    attempt.paths.role("work"), spec, run.history
                )
                copied_log = attempt.copy_file(
                    "logs",
                    ("xrun.log",),
                    source_log,
                    label="archived ADE Xcelium log",
                )
                text = read_nofollow_text(copied_log, errors="replace")
                validate_ade_simulator_success(text)
                rows, summary = parse_results(text)
                truth_table = attempt.write_text(
                    "results",
                    ("truth_table.csv",),
                    render_truth_table(rows),
                    label="verified ADE truth table",
                )
                validate_truth_contract(spec, text, rows, summary)
                assert summary is not None
                commit_details.update({
                    "history": run.history,
                    "simulator_status": run.status,
                    "worker_terminated_after_completion": (
                        run.terminated_after_completion
                    ),
                    "source_log": str(source_log),
                    "vectors": summary["vectors"],
                })
                truth_table_for_commit = truth_table
                result = AdeRunResult(
                    namespace_dir=namespace.root,
                    setup_dir=committed_setup.setup_dir,
                    run_dir=attempt.paths.root,
                    history=run.history,
                    simulator_status=run.status,
                    source_log=source_log,
                    copied_log=copied_log,
                    truth_table=truth_table,
                    vectors=summary["vectors"],
                    run_fingerprint=run_fingerprint,
                )
            except BaseException as exc:
                body_failure = exc
                if run is not None:
                    commit_details.update({
                        "history": run.history,
                        "simulator_status": run.status,
                    })
                if not isinstance(exc, Exception):
                    raise
                raise RuntimeError(
                    f"{exc}; ADE run directory retained at {attempt.paths.root}"
                ) from exc
    if result is None:
        raise RuntimeError("ADE run completed without a result")
    return result
