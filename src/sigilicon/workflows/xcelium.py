"""Managed Xcelium execution for source-owned RTL verification cells."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
import tomllib
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.project import Project
from sigilicon.domain.verification_cell import VerificationCellSpec, load_verification_cell
from sigilicon.external_tools import (
    ProcessPort,
    ProcessRequest,
    managed_process,
    owned_directory,
    xrun_env,
)
from sigilicon.execution._workspace import StepWorkspace
from sigilicon.execution._model import Resources, json_value


_HDL_SOURCE_SUFFIXES = frozenset({".sv", ".svh", ".v", ".vh"})


@dataclass(frozen=True)
class XceliumCellPlan:
    """Resolved source and invocation plan for one verification cell."""

    contract: Path
    spec: VerificationCellSpec
    sources: tuple[Path, ...]
    command_template: tuple[str, ...]
    source_records: Mapping[Path, str]

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
class XceliumExecution:
    """Common Xcelium result for digital or AMS verification."""

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


def resolve_xcelium_contract(path: Path, *, project: Project) -> Path:
    """Resolve one verification cell inside an explicit Project owner."""

    root = project.project_root
    contract = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not contract.is_relative_to(root) or not contract.is_file():
        raise ValueError(f"Xcelium verification cell contract is not project-owned: {path}")
    project.require_owner(contract)
    return contract


def snapshot_verification_sources(
    paths: set[Path],
    *,
    documents: tuple[Mapping[Path, Mapping[str, Any]], ...],
) -> Mapping[Path, str]:
    """Bind exact source bytes to the semantic documents used by a planner."""

    records: dict[Path, str] = {}
    for source in paths:
        path = Path(source).absolute()
        if path != path.resolve():
            raise ValueError(f"verification source traverses a symlink: {path}")
        records[path] = read_nofollow_text(path)
    for inventory in documents:
        for path, document in inventory.items():
            resolved = path.absolute()
            try:
                parsed = tomllib.loads(records[resolved])
            except (KeyError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(
                    f"verification source document snapshot drift: {resolved}"
                ) from exc
            if json_value(parsed) != json_value(document):
                raise ValueError(
                    f"verification source document snapshot drift: {resolved}"
                )
    return MappingProxyType(records)


def plan_xcelium_cell(
    contract_path: Path,
    *,
    project: Project,
) -> XceliumCellPlan:
    """Resolve a cell without finding or launching an external simulator."""

    repository = project
    contract = resolve_xcelium_contract(contract_path, project=repository)
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
        *(path.relative_to(repository.project_root).as_posix() for path in sources),
    )
    source_paths = {
        contract,
        *spec.source_inputs,
        *spec.source_documents,
    }
    if spec.runner is not None:
        source_paths.add(spec.runner)
    return XceliumCellPlan(
        contract=contract,
        spec=spec,
        sources=sources,
        command_template=command_template,
        source_records=snapshot_verification_sources(
            source_paths,
            documents=(spec.source_documents,),
        ),
    )


def execute_xcelium_cell(
    plan: XceliumCellPlan,
    *,
    artifacts: StepWorkspace,
    resources: Resources,
    before_spawn: Callable[[], None] | None = None,
    environment_values: Mapping[str, str] | None = None,
    timeout: int = 600,
    process: ProcessPort = managed_process,
) -> XceliumExecution:
    """Execute a resolved cell without creating or completing a run record."""

    return execute_xcelium_invocation(
        artifacts=artifacts,
        plan_record=plan.as_dict(),
        cell=plan.spec.cell,
        dut=plan.spec.dut,
        success_marker=plan.spec.success_marker,
        command_factory=lambda xrun_bin, work_path, xcelium_path: [
            str(xrun_bin),
            "-64bit",
            "-sv",
            "-timescale",
            "1ns/1ps",
            "-xmlibdirname",
            xcelium_path,
            "-log",
            f"{work_path}/xrun.log",
            *(str(path) for path in plan.sources),
        ],
        validate_inputs=lambda: _require_xcelium_sources(plan),
        before_spawn=before_spawn,
        environment_values=environment_values,
        resources=resources,
        timeout=timeout,
        process=process,
    )


def _require_xcelium_sources(plan: XceliumCellPlan) -> None:
    for source_input in plan.spec.source_inputs:
        if not source_input.is_file():
            raise FileNotFoundError(
                f"Xcelium source input disappeared: {source_input}"
            )


def execute_xcelium_invocation(
    *,
    artifacts: StepWorkspace,
    plan_record: Mapping[str, object],
    cell: str,
    dut: str,
    success_marker: str,
    command_factory: Callable[[Path, str, str], list[str]],
    prepare_inputs: Callable[[], None] | None = None,
    validate_inputs: Callable[[], None] | None = None,
    summary_fields: Mapping[str, object] | None = None,
    resources: Resources,
    before_spawn: Callable[[], None] | None = None,
    environment_values: Mapping[str, str] | None = None,
    timeout: int = 600,
    process: ProcessPort = managed_process,
) -> XceliumExecution:
    """Run one typed Xcelium plan after its caller-specific inputs are prepared."""

    selected_environment = (
        {} if environment_values is None else dict(environment_values)
    )
    xrun_bin = resources.require_tool("cadence.xrun")
    if prepare_inputs is not None:
        prepare_inputs()
    artifacts.write_json(
        "inputs",
        ("source-manifest.json",),
        {"schema": 1, "plan": plan_record},
    )
    work_dir = artifacts.directory("work")
    xcelium_dir = artifacts.directory("work", "xcelium.d")
    with (
        resources.owned_tool("cadence.xrun") as owned_xrun,
        owned_directory(work_dir) as owned_work,
        owned_directory(xcelium_dir) as owned_xcelium,
    ):
        requested_command = command_factory(
            xrun_bin,
            owned_work.child_path,
            owned_xcelium.child_path,
        )
        command = [*owned_xrun.command, *requested_command[1:]]

        def validate_spawn() -> None:
            owned_xrun.require_visible()
            owned_work.require_visible()
            owned_xcelium.require_visible()
            if before_spawn is not None:
                before_spawn()
            if validate_inputs is not None:
                validate_inputs()

        completed = process.run(ProcessRequest(
            argv=tuple(command),
            executable=owned_xrun.executable,
            cwd=Path(owned_work.child_path),
            environment=xrun_env(xrun_bin, selected_environment),
            timeout_seconds=timeout,
            before_spawn=validate_spawn,
            pass_fds=(owned_work.fd, owned_xcelium.fd),
        ))
        native_log_bytes = owned_work.read_child_bytes(
            "xrun.log",
            missing_ok=True,
        )
    stdout_path = artifacts.write_text(
        "logs", ("xrun.stdout.log",), completed.stdout
    )
    stderr_path = artifacts.write_text(
        "logs", ("xrun.stderr.log",), completed.stderr
    )
    native_output = (
        native_log_bytes.decode("utf-8", errors="replace")
        if native_log_bytes is not None
        else ""
    )
    native_log_path = (
        artifacts.write_bytes("logs", ("xrun.log",), native_log_bytes)
        if native_log_bytes is not None
        else None
    )
    success_marker_evidence = [
        source
        for source, output in (
            ("stdout", completed.stdout),
            ("native_log", native_output),
        )
        if success_marker in output
    ]
    success_marker_seen = bool(success_marker_evidence)
    passed = completed.returncode == 0 and success_marker_seen
    summary = {
        "schema": 1,
        "cell": cell,
        "dut": dut,
        "xrun": str(xrun_bin),
        "command": command,
        "returncode": completed.returncode,
        "success_marker": success_marker,
        "success_marker_seen": success_marker_seen,
        "success_marker_evidence": success_marker_evidence,
        "passed": passed,
        **(summary_fields or {}),
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
        "outputs", ("summary.json",), summary
    )
    return XceliumExecution(
        run_summary=summary_path,
        returncode=completed.returncode,
        passed=passed,
        stdout=completed.stdout,
        stderr=completed.stderr,
        native_log=native_output,
    )
