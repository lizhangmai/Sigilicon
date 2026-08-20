"""Managed Xcelium execution for source-owned RTL verification cells."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from sigilicon.artifacts import ArtifactRecord, file_sha256, new_identity
from sigilicon.domain.fingerprints import source_fingerprint_set
from sigilicon.domain.provenance import digest
from sigilicon.domain.verification_cell import VerificationCellSpec, load_verification_cell
from sigilicon.external_tools import run_process_group_capture
from sigilicon.paths import ProjectContext
from sigilicon.workflows.ams_standalone import find_xrun, xrun_env
from sigilicon.workflows.source_control import inspect_source_state


@dataclass(frozen=True)
class XceliumCellPlan:
    """Resolved source and invocation plan for one verification cell."""

    contract: Path
    spec: VerificationCellSpec
    sources: tuple[Path, ...]
    source_fingerprints: Any
    command_template: tuple[str, ...]

    def as_dict(self, *, project_root: Path) -> dict[str, object]:
        root = project_root.resolve()
        return {
            "cell": self.spec.cell,
            "dut": self.spec.dut,
            "simulator": self.spec.simulator,
            "contract": self.contract.relative_to(root).as_posix(),
            "canonical_source": self.spec.canonical_source.relative_to(root).as_posix(),
            "dependencies": [
                path.relative_to(root).as_posix() for path in self.spec.dependencies
            ],
            "runner": (
                self.spec.runner.relative_to(root).as_posix()
                if self.spec.runner is not None
                else None
            ),
            "sources": [path.relative_to(root).as_posix() for path in self.sources],
            "source_fingerprint": self.source_fingerprints.exact,
            "semantic_fingerprint": self.source_fingerprints.semantic,
            "command_template": list(self.command_template),
        }


@dataclass(frozen=True)
class XceliumCellRun:
    """Completed managed Xcelium invocation."""

    plan: XceliumCellPlan
    manifest: Path
    returncode: int
    stdout: str
    stderr: str


def _resolve_contract(path: Path, *, project_root: Path) -> Path:
    root = project_root.resolve()
    contract = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not contract.is_relative_to(root) or not contract.is_file():
        raise ValueError(f"Xcelium verification cell contract is not project-owned: {path}")
    return contract


def plan_xcelium_cell(
    contract_path: Path,
    *,
    project_root: Path,
) -> XceliumCellPlan:
    """Resolve a cell without finding or launching an external simulator."""

    contract = _resolve_contract(contract_path, project_root=project_root)
    spec = load_verification_cell(contract, project_root=project_root)
    if spec.simulator.lower() != "xcelium":
        raise ValueError(
            f"verification cell {spec.cell} declares simulator {spec.simulator!r}, "
            "not xcelium"
        )
    sources = (spec.canonical_source, *spec.dependencies)
    if len(set(sources)) != len(sources):
        raise ValueError(f"verification cell {spec.cell} has duplicate compile sources")
    fingerprint_inputs: dict[str, Path] = {
        "contract/cell.toml": contract,
        "source/canonical": spec.canonical_source,
    }
    fingerprint_inputs.update(
        {
            f"source/dependency/{index}": path
            for index, path in enumerate(spec.dependencies)
        }
    )
    if spec.runner is not None:
        fingerprint_inputs["runner/cell"] = spec.runner
    fingerprints = source_fingerprint_set(fingerprint_inputs)
    command_template = (
        "xrun",
        "-64bit",
        "-sv",
        "-timescale",
        "1ns/1ps",
        "-xmlibdirname",
        "$RUN_WORK/xcelium.d",
        "-log",
        "$RUN_WORK/xrun.log",
        *(str(path) for path in sources),
    )
    return XceliumCellPlan(
        contract=contract,
        spec=spec,
        sources=sources,
        source_fingerprints=fingerprints,
        command_template=command_template,
    )


def run_xcelium_cell(
    contract_path: Path,
    *,
    project_root: Path,
    artifact_root: Path,
    xrun: Path | None = None,
    timeout: int = 600,
) -> XceliumCellRun:
    """Run one verification cell through an isolated artifact work directory."""

    root = project_root.resolve()
    plan = plan_xcelium_cell(contract_path, project_root=root)
    xrun_bin = find_xrun(xrun)
    source_state = inspect_source_state(root).as_dict()
    paths = ProjectContext.from_project_root(root, artifact_root=artifact_root)
    run_id = new_identity()
    setup_fingerprint = digest(
        {
            "simulator": plan.spec.simulator,
            "dut": plan.spec.dut,
            "options": list(plan.command_template[:9]),
            "timeout": timeout,
        }
    )
    run_fingerprint = digest(
        {
            "source": plan.source_fingerprints.exact,
            "semantic": plan.source_fingerprints.semantic,
            "setup": setup_fingerprint,
        }
    )
    attempt = ArtifactRecord.begin(
        paths.artifacts.standalone_run(
            paths.artifact_namespace, plan.spec.cell, run_id
        ),
        entities={
            "library": paths.artifact_namespace,
            "cell": plan.spec.dut,
            "testbench": plan.spec.cell,
        },
        operation="simulate",
        backend="xcelium-rtl-cell",
        source_fingerprint=plan.source_fingerprints.exact,
        semantic_fingerprint=plan.source_fingerprints.semantic,
        setup_fingerprint=setup_fingerprint,
        run_fingerprint=run_fingerprint,
    )

    try:
        attempt.write_json(
            "inputs",
            ("source-manifest.json",),
            {
                "schema": 1,
                "plan": plan.as_dict(project_root=root),
                "source_state": source_state,
                "source_fingerprints": {
                    "exact": plan.source_fingerprints.exact_inputs,
                    "semantic": plan.source_fingerprints.semantic_inputs,
                },
            },
            label="Xcelium source and source-state snapshot",
        )
        work_dir = attempt.directory("work")
        xcelium_dir = attempt.directory("work", "xcelium.d")
        command = [
            str(xrun_bin),
            "-64bit",
            "-sv",
            "-timescale",
            "1ns/1ps",
            "-xmlibdirname",
            str(xcelium_dir),
            "-log",
            str(work_dir / "xrun.log"),
            *(str(path) for path in plan.sources),
        ]

        def validate_spawn() -> None:
            for source in plan.sources:
                if not source.is_file():
                    raise FileNotFoundError(f"Xcelium source disappeared: {source}")

        completed = run_process_group_capture(
            command,
            cwd=work_dir,
            env=xrun_env(xrun_bin),
            timeout=timeout,
            before_spawn=validate_spawn,
        )
        stdout_path = attempt.write_text(
            "logs", ("xrun.stdout.log",), completed.stdout, label="Xcelium stdout"
        )
        stderr_path = attempt.write_text(
            "logs", ("xrun.stderr.log",), completed.stderr, label="Xcelium stderr"
        )
        native_log = work_dir / "xrun.log"
        native_log_path = (
            attempt.copy_file("logs", ("xrun.log",), native_log, label="Xcelium log")
            if native_log.is_file()
            else None
        )
        summary = {
            "schema": 1,
            "cell": plan.spec.cell,
            "dut": plan.spec.dut,
            "xrun": str(xrun_bin),
            "command": command,
            "returncode": completed.returncode,
            "passed": completed.returncode == 0,
            "source_state": source_state,
            "logs": {
                "stdout": str(stdout_path.relative_to(attempt.paths.root)),
                "stderr": str(stderr_path.relative_to(attempt.paths.root)),
                "native": (
                    str(native_log_path.relative_to(attempt.paths.root))
                    if native_log_path is not None
                    else None
                ),
            },
        }
        summary_path = attempt.write_json(
            "results", ("summary.json",), summary, label="Xcelium completion summary"
        )
        if completed.returncode != 0:
            error = RuntimeError(
                f"xrun failed for {plan.spec.cell} with exit code {completed.returncode}"
            )
            attempt.fail(error, details={"summary": summary})
        else:
            attempt.succeed(
                completion_evidence=(summary_path,),
                details={
                    "product_qualification_conclusion": False,
                    "stdout_sha256": file_sha256(stdout_path),
                    "stderr_sha256": file_sha256(stderr_path),
                },
            )
        return XceliumCellRun(
            plan=plan,
            manifest=attempt.paths.manifest,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except BaseException as error:
        if attempt.status == "running":
            attempt.fail(error)
        raise
