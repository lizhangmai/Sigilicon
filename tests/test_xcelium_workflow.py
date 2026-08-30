from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sigilicon.artifacts import load_manifest
from sigilicon.cli.xcelium import _display_path
from sigilicon.domain.repository import Project
import sigilicon.domain.repository as repository_module
from sigilicon.workflows import xcelium
from sigilicon.workflows.xcelium import (
    plan_xcelium_cell,
    run_xcelium_cell,
)

from conftest import write_component_owner


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_xcelium_cli_paths_support_external_artifact_roots(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    local = project_root / "build/run/manifest.json"
    external = tmp_path / "external/run/manifest.json"

    assert _display_path(local, project_root=project_root) == (
        "build/run/manifest.json"
    )
    assert _display_path(external, project_root=project_root) == str(external)


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

    project = Project.from_file(project_contract)
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
            project=Project.from_file(tmp_path / "sigilicon.toml"),
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
            project=Project.from_file(tmp_path / "sigilicon.toml"),
        )


def test_xcelium_plan_validates_declared_contract_header(tmp_path: Path) -> None:
    contract = _verification_project(tmp_path)
    interface = tmp_path / "ip/demo/configs/interface.toml"
    interface.write_text('schema = 1\nowner = "demo"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="contract_kind must be a non-empty string"):
        plan_xcelium_cell(
            contract,
            project=Project.from_file(tmp_path / "sigilicon.toml"),
        )


def test_xcelium_run_reuses_project_and_writes_managed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.from_project_root(tmp_path)
    xrun = _write(tmp_path / "tools/xcelium/tools/bin/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(command, *, cwd, before_spawn, **_kwargs):
        assert command[0] == str(xrun)
        before_spawn()
        (cwd / "xrun.log").write_text("fixture Xcelium log\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command, 0, "TB_DEMO_SUMMARY failures=0\n", ""
        )

    monkeypatch.setattr(xcelium, "run_process_group_capture", capture)
    monkeypatch.setattr(xcelium, "new_identity", lambda: "3" * 32)

    result = run_xcelium_cell(contract, project=project, xrun=xrun, timeout=17)

    assert result.returncode == 0
    assert result.passed
    assert result.plan.spec.project is project
    assert result.run_id == "3" * 32
    assert result.run_dir == result.manifest_path.parent
    assert result.run_summary == result.run_dir / "outputs/summary.json"
    assert result.manifest == result.manifest_path
    manifest = load_manifest(result.manifest_path)
    assert manifest["status"] == "succeeded"
    assert manifest["operation_id"] == "3" * 32
    assert manifest["entities"]["library"] == "demo"
    assert manifest["completion_evidence"] == ["outputs/summary.json"]


def test_xcelium_run_fails_artifact_when_success_marker_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.from_project_root(tmp_path)
    xrun = _write(tmp_path / "tools/xcelium/tools/bin/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(command, *, cwd, before_spawn, **_kwargs):
        before_spawn()
        return subprocess.CompletedProcess(command, 0, "FAIL fixture assertion\n", "")

    monkeypatch.setattr(xcelium, "run_process_group_capture", capture)
    monkeypatch.setattr(xcelium, "new_identity", lambda: "4" * 32)

    result = run_xcelium_cell(contract, project=project, xrun=xrun)

    assert result.returncode == 0
    assert not result.passed
    manifest = load_manifest(result.manifest_path)
    assert manifest["status"] == "failed"
    assert manifest["operation_id"] == "4" * 32
    assert manifest["details"]["summary"]["success_marker_seen"] is False


def test_xcelium_run_accepts_success_marker_from_native_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _verification_project(tmp_path)
    project = Project.from_project_root(tmp_path)
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
    monkeypatch.setattr(xcelium, "new_identity", lambda: "5" * 32)

    result = run_xcelium_cell(contract, project=project, xrun=xrun)

    assert result.passed
    assert "TB_DEMO_SUMMARY failures=0" in result.evidence_output
    manifest = load_manifest(result.manifest_path)
    assert manifest["status"] == "succeeded"
    assert manifest["operation_id"] == "5" * 32
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_evidence"] == ["native_log"]
