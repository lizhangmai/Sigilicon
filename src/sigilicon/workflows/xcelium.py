"""Managed Xcelium execution for source-owned RTL verification cells."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sigilicon.domain.repository import Project
from sigilicon.domain.verification_cell import VerificationCellSpec, load_verification_cell
from sigilicon.external_tools import (
    find_xrun,
    owned_directory,
    run_process_group_capture,
    xrun_env,
)
from sigilicon.workflows.run_artifacts import RunArtifacts


_HDL_SOURCE_SUFFIXES = frozenset({".sv", ".svh", ".v", ".vh"})


@dataclass(frozen=True)
class XceliumCellPlan:
    """Resolved source and invocation plan for one verification cell."""

    contract: Path
    spec: VerificationCellSpec
    sources: tuple[Path, ...]
    command_template: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        root = self.spec.project_root
        return {
            "owner": self.spec.owner,
            "cell": self.spec.cell,
            "dut": self.spec.dut,
            "simulator": self.spec.simulator,
            "contract": self.contract.relative_to(root).as_posix(),
            "canonical_source": self.spec.canonical_source.relative_to(root).as_posix(),
            "compile_sources": [
                path.relative_to(root).as_posix() for path in self.spec.compile_sources
            ],
            "support_files": [
                path.relative_to(root).as_posix() for path in self.spec.support_files
            ],
            "contracts": [
                path.relative_to(root).as_posix() for path in self.spec.contracts
            ],
            "runner": (
                self.spec.runner.relative_to(root).as_posix()
                if self.spec.runner is not None
                else None
            ),
            "sources": [path.relative_to(root).as_posix() for path in self.sources],
            "success_marker": self.spec.success_marker,
            "command_template": list(self.command_template),
        }


@dataclass(frozen=True)
class XceliumCellExecution:
    """Tool result written into an execution lifecycle owned by the caller."""

    plan: XceliumCellPlan
    run_summary: Path
    returncode: int
    passed: bool
    stdout: str
    stderr: str
    native_log: str

    @property
    def evidence_output(self) -> str:
        """Combined simulator output available to owner-specific result parsers."""

        return "\n".join(output for output in (self.stdout, self.native_log) if output)


def _resolve_contract(path: Path, *, project: Project) -> Path:
    root = project.project_root
    contract = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not contract.is_relative_to(root) or not contract.is_file():
        raise ValueError(f"Xcelium verification cell contract is not project-owned: {path}")
    project.require_owner(contract)
    return contract


def plan_xcelium_cell(
    contract_path: Path,
    *,
    project: Project,
) -> XceliumCellPlan:
    """Resolve a cell without finding or launching an external simulator."""

    repository = project
    contract = _resolve_contract(contract_path, project=repository)
    spec = load_verification_cell(contract, project=repository)
    if spec.simulator.lower() != "xcelium":
        raise ValueError(
            f"verification cell {spec.cell} declares simulator {spec.simulator!r}, "
            "not xcelium"
        )
    if spec.success_marker is None:
        raise ValueError(
            f"Xcelium verification cell {spec.cell} must declare success_marker"
        )
    sources = (spec.canonical_source, *spec.compile_sources)
    if len(set(sources)) != len(sources):
        raise ValueError(f"verification cell {spec.cell} has duplicate compile sources")
    invalid_sources = [
        path for path in sources if path.suffix.lower() not in _HDL_SOURCE_SUFFIXES
    ]
    if invalid_sources:
        invalid = ", ".join(
            path.relative_to(repository.project_root).as_posix()
            for path in invalid_sources
        )
        raise ValueError(
            "Xcelium compile inputs must be Verilog/SystemVerilog sources; "
            f"declare non-HDL inputs in support_files or contracts: {invalid}"
        )
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
        command_template=command_template,
    )


def execute_xcelium_cell(
    plan: XceliumCellPlan,
    *,
    artifacts: RunArtifacts,
    xrun: Path | None = None,
    timeout: int = 600,
) -> XceliumCellExecution:
    """Execute a resolved cell without creating or completing a run record."""

    xrun_bin = find_xrun(xrun)
    artifacts.write_json(
        "inputs",
        ("source-manifest.json",),
        {"schema": 1, "plan": plan.as_dict()},
        label="Xcelium source plan",
    )
    work_dir = artifacts.directory("work")
    xcelium_dir = artifacts.directory("work", "xcelium.d")
    with owned_directory(work_dir) as owned_work, owned_directory(
        xcelium_dir
    ) as owned_xcelium:
        command = [
            str(xrun_bin),
            "-64bit",
            "-sv",
            "-timescale",
            "1ns/1ps",
            "-xmlibdirname",
            owned_xcelium.child_path,
            "-log",
            owned_work.child_file("xrun.log"),
            *(str(path) for path in plan.sources),
        ]

        def validate_spawn() -> None:
            owned_work.require_visible()
            owned_xcelium.require_visible()
            for source_input in plan.spec.source_inputs:
                if not source_input.is_file():
                    raise FileNotFoundError(
                        f"Xcelium source input disappeared: {source_input}"
                    )

        completed = run_process_group_capture(
            command,
            cwd=Path(owned_work.child_path),
            env=xrun_env(xrun_bin),
            timeout=timeout,
            before_spawn=validate_spawn,
            pass_fds=(owned_work.fd, owned_xcelium.fd),
        )
    stdout_path = artifacts.write_text(
        "logs", ("xrun.stdout.log",), completed.stdout, label="Xcelium stdout"
    )
    stderr_path = artifacts.write_text(
        "logs", ("xrun.stderr.log",), completed.stderr, label="Xcelium stderr"
    )
    native_log = work_dir / "xrun.log"
    native_output = (
        native_log.read_text(encoding="utf-8", errors="replace")
        if native_log.is_file()
        else ""
    )
    native_log_path = (
        artifacts.copy_file("logs", ("xrun.log",), native_log, label="Xcelium log")
        if native_log.is_file()
        else None
    )
    success_marker_evidence = [
        source
        for source, output in (
            ("stdout", completed.stdout),
            ("native_log", native_output),
        )
        if plan.spec.success_marker in output
    ]
    success_marker_seen = bool(success_marker_evidence)
    passed = completed.returncode == 0 and success_marker_seen
    summary = {
        "schema": 1,
        "cell": plan.spec.cell,
        "dut": plan.spec.dut,
        "xrun": str(xrun_bin),
        "command": command,
        "returncode": completed.returncode,
        "success_marker": plan.spec.success_marker,
        "success_marker_seen": success_marker_seen,
        "success_marker_evidence": success_marker_evidence,
        "passed": passed,
        "logs": {
            "stdout": str(stdout_path.relative_to(artifacts.root)),
            "stderr": str(stderr_path.relative_to(artifacts.root)),
            "native": (
                str(native_log_path.relative_to(artifacts.root))
                if native_log_path is not None
                else None
            ),
        },
    }
    summary_path = artifacts.write_json(
        "outputs", ("summary.json",), summary, label="Xcelium completion summary"
    )
    return XceliumCellExecution(
        plan=plan,
        run_summary=summary_path,
        returncode=completed.returncode,
        passed=passed,
        stdout=completed.stdout,
        stderr=completed.stderr,
        native_log=native_output,
    )
