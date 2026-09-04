from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.project import Project
from sigilicon.workflows import xcelium as xcelium_workflow
from sigilicon.external_tools import ProcessResult
from sigilicon.execution._model import Resources
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.workflows.xcelium import (
    execute_xcelium_cell,
    plan_xcelium_cell,
)

from conftest import write_component_owner


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run_artifacts(root: Path) -> ExecutionWorkspace:
    run = root / "run"
    return ExecutionWorkspace(
        run_id="managed-run",
        root=run,
        input_root=run / "work/action/inputs",
        work_root=run / "work/action/tool",
        output_root=run / "outputs/action/evidence",
        log_root=run / "logs/action",
        source={},
    )


def _verification_project(root: Path) -> Path:
    cell = root / "ip/demo/verification/tb_demo"
    _write(cell / "testbench.sv", "module tb_demo; endmodule\n")
    _write(root / "ip/demo/rtl/dut.sv", "module dut; endmodule\n")
    _write(
        root / "ip/demo/configs/interface.toml",
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "demo"
''',
    )
    contract = _write(
        cell / "cell.toml",
        '''schema = 1
contract_kind = "verification-cell"
path_scope = "cell"
owner = "demo"

cell = "tb_demo"
role = "rtl-testbench"
canonical_source = "testbench.sv"
dut = "dut"
simulator = "xcelium"
compile_sources = ["../../rtl/dut.sv"]
contracts = ["../../configs/interface.toml"]
success_marker = "TB_DEMO_SUMMARY failures=0"
''',
    )
    write_component_owner(
        root,
        "demo",
        filesets={
            "verification": (
                "ip/demo/verification/tb_demo/cell.toml",
                "ip/demo/verification/tb_demo/testbench.sv",
                "ip/demo/rtl/dut.sv",
                "ip/demo/configs/interface.toml",
            ),
        },
    )
    return contract


def test_project_xcelium_plan_reuses_the_explicit_project(tmp_path: Path) -> None:
    contract = _verification_project(tmp_path)
    project = Project.open(tmp_path)
    plan = plan_xcelium_cell(contract, project=project)
    payload = plan.as_dict()

    assert plan.spec.project_root == tmp_path
    assert plan.spec.project is project
    assert plan.spec.owner == "demo"
    assert payload["contract"] == "ip/demo/verification/tb_demo/cell.toml"
    assert payload["owner"] == "demo"
    assert payload["contracts"] == ["ip/demo/configs/interface.toml"]
    assert all(not source.endswith(".toml") for source in payload["sources"])


def test_xcelium_plan_rejects_non_hdl_compile_dependency(tmp_path: Path) -> None:
    contract = _verification_project(tmp_path)
    _write(tmp_path / "ip/demo/configs/other.toml", "schema = 1\n")
    text = contract.read_text(encoding="utf-8")
    contract.write_text(
        text.replace(
            'compile_sources = ["../../rtl/dut.sv"]',
            'compile_sources = ["../../rtl/dut.sv", "../../configs/other.toml"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="support_files or contracts"):
        plan_xcelium_cell(
            contract,
            project=Project.open(tmp_path),
        )


def test_xcelium_plan_requires_explicit_success_marker(tmp_path: Path) -> None:
    contract = _verification_project(tmp_path)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'success_marker = "TB_DEMO_SUMMARY failures=0"\n',
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must declare success_marker"):
        plan_xcelium_cell(
            contract,
            project=Project.open(tmp_path),
        )


def test_xcelium_execution_reuses_plan_and_writes_flow_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.open(tmp_path)
    xrun = _write(tmp_path / "tools/xcelium/tools/bin/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(request):
        assert str(xrun) in request.argv
        assert str(request.cwd).startswith("/proc/") and "/fd/" in str(request.cwd)
        assert len(request.pass_fds) == 2
        request.before_spawn()
        (request.cwd / "xrun.log").write_text(
            "fixture Xcelium log\n", encoding="utf-8"
        )
        return ProcessResult(
            returncode=0,
            stdout="TB_DEMO_SUMMARY failures=0\n",
            stderr="",
        )

    plan = plan_xcelium_cell(contract, project=project)
    artifacts = _run_artifacts(tmp_path)
    result = execute_xcelium_cell(
        plan,
        artifacts=artifacts,
        resources=Resources(tools={"cadence.xrun": str(xrun)}),
        timeout=17,
        process=SimpleNamespace(run=capture),
    )

    assert result.returncode == 0
    assert result.passed
    assert result.run_summary == artifacts.path("outputs", "summary.json")
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_seen"] is True


def test_xcelium_execution_reports_absent_success_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.open(tmp_path)
    xrun = _write(tmp_path / "tools/xcelium/tools/bin/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(request):
        request.before_spawn()
        return ProcessResult(
            returncode=0,
            stdout="FAIL fixture assertion\n",
            stderr="",
        )

    result = execute_xcelium_cell(
        plan_xcelium_cell(contract, project=project),
        artifacts=_run_artifacts(tmp_path),
        resources=Resources(tools={"cadence.xrun": str(xrun)}),
        process=SimpleNamespace(run=capture),
    )

    assert result.returncode == 0
    assert not result.passed
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_seen"] is False


def test_xcelium_execution_accepts_success_marker_from_native_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.open(tmp_path)
    xrun = _write(tmp_path / "tools/xcelium/tools/bin/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(request):
        request.before_spawn()
        (request.cwd / "xrun.log").write_text(
            "TB_DEMO_SUMMARY failures=0\n",
            encoding="utf-8",
        )
        return ProcessResult(returncode=0, stdout="", stderr="")

    result = execute_xcelium_cell(
        plan_xcelium_cell(contract, project=project),
        artifacts=_run_artifacts(tmp_path),
        resources=Resources(tools={"cadence.xrun": str(xrun)}),
        process=SimpleNamespace(run=capture),
    )

    assert result.passed
    assert "TB_DEMO_SUMMARY failures=0" in result.evidence_output
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_evidence"] == ["native_log"]


def test_xcelium_captures_native_log_before_releasing_work_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.open(tmp_path)
    xrun = _write(tmp_path / "tools/xcelium/tools/bin/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)
    artifacts = _run_artifacts(tmp_path)
    original_owned_directory = xcelium_workflow.owned_directory

    @contextmanager
    def replace_log_after_guard(path: Path, *, create_missing: bool = False):
        with original_owned_directory(
            path,
            create_missing=create_missing,
        ) as owned:
            yield owned
        if Path(path) == artifacts.work_root:
            (Path(path) / "xrun.log").write_text(
                "TB_DEMO_SUMMARY failures=0\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        xcelium_workflow,
        "owned_directory",
        replace_log_after_guard,
    )

    def capture(request):
        request.before_spawn()
        (request.cwd / "xrun.log").write_text(
            "original native log without marker\n",
            encoding="utf-8",
        )
        return ProcessResult(returncode=0, stdout="", stderr="")

    result = execute_xcelium_cell(
        plan_xcelium_cell(contract, project=project),
        artifacts=artifacts,
        resources=Resources(tools={"cadence.xrun": str(xrun)}),
        process=SimpleNamespace(run=capture),
    )

    assert result.native_log == "original native log without marker\n"
    assert not result.passed
