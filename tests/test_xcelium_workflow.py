from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sigilicon.project import Project
import sigilicon.project._project as repository_module
from sigilicon.workflows import xcelium
from sigilicon.workflows.run_artifacts import RunArtifacts
from sigilicon.workflows.xcelium import (
    execute_xcelium_cell,
    plan_xcelium_cell,
)

from conftest import write_component_owner


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run_artifacts(root: Path) -> RunArtifacts:
    run = root / "run"
    return RunArtifacts(
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


def test_project_xcelium_plan_parses_one_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project_contract = (tmp_path / "sigilicon.toml").resolve()
    original = repository_module.read_toml
    manifest_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == project_contract:
            manifest_reads += 1
        return original(path)

    monkeypatch.setattr(repository_module, "read_toml", counted)

    project = Project.open(project_contract.parent)
    plan = plan_xcelium_cell(contract, project=project)
    payload = plan.as_dict()

    assert plan.spec.project_root == tmp_path
    assert plan.spec.project is project
    assert plan.spec.owner == "demo"
    assert payload["contract"] == "ip/demo/verification/tb_demo/cell.toml"
    assert payload["owner"] == "demo"
    assert payload["contracts"] == ["ip/demo/configs/interface.toml"]
    assert all(not source.endswith(".toml") for source in payload["sources"])
    assert manifest_reads == 1


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

    def capture(command, *, cwd, before_spawn, **kwargs):
        assert command[0] == str(xrun)
        assert str(cwd).startswith("/proc/") and "/fd/" in str(cwd)
        assert len(kwargs["pass_fds"]) == 2
        before_spawn()
        (cwd / "xrun.log").write_text("fixture Xcelium log\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command, 0, "TB_DEMO_SUMMARY failures=0\n", ""
        )

    monkeypatch.setattr(xcelium, "run_process_group_capture", capture)
    plan = plan_xcelium_cell(contract, project=project)
    artifacts = _run_artifacts(tmp_path)
    result = execute_xcelium_cell(
        plan,
        artifacts=artifacts,
        xrun=xrun,
        timeout=17,
    )

    assert result.returncode == 0
    assert result.passed
    assert result.plan.spec.project is project
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

    def capture(command, *, cwd, before_spawn, **_kwargs):
        before_spawn()
        return subprocess.CompletedProcess(command, 0, "FAIL fixture assertion\n", "")

    monkeypatch.setattr(xcelium, "run_process_group_capture", capture)
    result = execute_xcelium_cell(
        plan_xcelium_cell(contract, project=project),
        artifacts=_run_artifacts(tmp_path),
        xrun=xrun,
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

    def capture(command, *, cwd, before_spawn, **_kwargs):
        before_spawn()
        (cwd / "xrun.log").write_text(
            "TB_DEMO_SUMMARY failures=0\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(xcelium, "run_process_group_capture", capture)
    result = execute_xcelium_cell(
        plan_xcelium_cell(contract, project=project),
        artifacts=_run_artifacts(tmp_path),
        xrun=xrun,
    )

    assert result.passed
    assert "TB_DEMO_SUMMARY failures=0" in result.evidence_output
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_evidence"] == ["native_log"]
