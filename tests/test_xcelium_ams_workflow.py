from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import subprocess
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.ip_integration import (
    IpIntegrationDependency,
    IpReleaseDependency,
    OaNativeReleaseInterfaceReference,
)
from sigilicon.domain.repository import Project
from sigilicon.workflows import xcelium_ams
from sigilicon.workflows.run_artifacts import RunArtifacts
from sigilicon.workflows.xcelium_ams import (
    execute_xcelium_ams_cell,
    plan_xcelium_ams_cell,
)

from conftest import write_component_owner, write_test_platform


def test_xcelium_ams_execution_requires_a_caller_owned_run() -> None:
    assert not hasattr(xcelium_ams, "run_xcelium_ams_cell")


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


def _ams_project(root: Path) -> tuple[Path, Path]:
    cell = root / "ip/demo/verification/native/tb_demo_ams"
    contract = _write(
        cell / "cell.toml",
        '''schema = 1
contract_kind = "verification-cell"
path_scope = "cell"
owner = "demo"

cell = "tb_demo_ams"
role = "ams-migration-testbench"
canonical_source = "testbench.vams"
dut = "native_adapter"
simulator = "xcelium-ams"
compile_sources = ["../../../rtl/native_adapter.sv"]
success_marker = "TB_DEMO_AMS_SUMMARY failures=0"

[ams]
platform = "testpdk"
model_set = "nominal"
integration_contract = "../../../component.toml"
variant = "no-recovery"
fileset = "ams"
dependency = "native-provider"
circuit_role = "circuit_netlist"
transient_stop = "1u"
ie_voltage = 0.9
''',
    )
    testbench = _write(cell / "testbench.vams", "module tb_demo_ams; endmodule\n")
    adapter = _write(root / "ip/demo/rtl/native_adapter.sv", "module native_adapter; endmodule\n")
    write_component_owner(
        root,
        "demo",
        filesets={
            "verification": tuple(
                path.relative_to(root).as_posix()
                for path in (contract, testbench, adapter)
            )
        },
    )
    write_test_platform(root)
    circuit = _write(
        root / "artifacts/releases/native-provider/circuit.scs",
        "simulator lang=spectre\nsubckt NATIVE_TOP A VSS\nends NATIVE_TOP\n",
    )
    return contract, circuit


def _patch_native_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    circuit: Path,
) -> None:
    interface = OaNativeReleaseInterfaceReference(
        kind="oa-native",
        library="native_provider",
        cell="NATIVE_TOP",
        schematic_view="schematic",
        layout_view="layout",
    )
    release = IpReleaseDependency(
        export="native-top",
        required_maturity="development",
        interface=interface,
        roles=("interface_contract", "circuit_netlist"),
        role_modules=MappingProxyType({}),
        role_exports=MappingProxyType({}),
    )
    dependency = IpIntegrationDependency(
        name="native-provider",
        component_contract=PurePosixPath("ip/native-provider/component.toml"),
        release=release,
    )
    monkeypatch.setattr(
        xcelium_ams,
        "load_ip_integration_contract",
        lambda *_args, **_kwargs: SimpleNamespace(
            release_dependencies=(dependency,)
        ),
    )
    relative = circuit.relative_to(root / "artifacts").as_posix()
    monkeypatch.setattr(
        xcelium_ams,
        "check_ip_integration",
        lambda *_args, **_kwargs: {
            "schema": 1,
            "contract_kind": "ip-integration-check",
            "owner": "demo",
            "ip": "demo",
            "variant": "no-recovery",
            "fileset": "ams",
            "dependency_lock": "ip/demo/dependency.lock.toml",
            "passed": True,
            "architecture": None,
            "dependency_releases": [
                {
                    "name": "native-provider",
                    "export": "native-top",
                    "release_id": "development-123456789abc",
                    "maturity": "development",
                    "manifest": "releases/native-provider/manifest.json",
                    "role_exports": {
                        "interface_contract": "native-top",
                        "circuit_netlist": "native-top",
                    },
                    "roles": {
                        "interface_contract": "releases/native-provider/interface.toml",
                        "circuit_netlist": relative,
                    },
                }
            ],
            "source_files": [
                "ip/demo/rtl/native_adapter.sv",
            ],
            "release_sources": [relative],
        },
    )


def test_xcelium_ams_plan_resolves_locked_circuit_and_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _patch_native_resolution(monkeypatch, root=tmp_path, circuit=circuit)

    plan = plan_xcelium_ams_cell(contract, project=Project.from_project_root(tmp_path))

    assert plan.native_cell == "NATIVE_TOP"
    assert plan.circuit_netlist == circuit
    assert plan.model_set.name == "nominal"
    assert plan.model_set.file.name == "model.scs"
    assert [path.suffix for path in plan.sources] == [".vams", ".sv"]
    assert ".scs" not in " ".join(plan.command_template[:-1])
    control = plan.render_ams_control()
    assert f'include "{circuit}"' in control
    assert "config cell=NATIVE_TOP use=spice" in control
    assert "tran tran stop=1u" in control
    assert plan.as_dict()["product_qualification_conclusion"] is False


def test_xcelium_ams_plan_rejects_spectre_compile_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _patch_native_resolution(monkeypatch, root=tmp_path, circuit=circuit)
    testbench = contract.parent / "testbench.vams"
    spectre = _write(contract.parent / "testbench.scs", testbench.read_text())
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'canonical_source = "testbench.vams"',
            f'canonical_source = "{spectre.name}"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="locked release role"):
        plan_xcelium_ams_cell(contract, project=Project.from_project_root(tmp_path))


def test_xcelium_ams_execution_stages_inputs_and_records_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _patch_native_resolution(monkeypatch, root=tmp_path, circuit=circuit)
    project = Project.from_project_root(tmp_path)
    xrun = _write(tmp_path / "tools/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(command, *, cwd, before_spawn, **kwargs):
        assert str(cwd).startswith("/proc/") and "/fd/" in str(cwd)
        assert len(kwargs["pass_fds"]) == 2
        before_spawn()
        control = Path(command[-1]).read_text(encoding="utf-8")
        assert "/inputs/release/circuit.scs" in control
        assert "/inputs/pdk/model.scs" in control
        assert str(circuit) not in control
        (cwd / "xrun.log").write_text(
            "TB_DEMO_AMS_SUMMARY failures=0\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(xcelium_ams, "run_process_group_capture", capture)
    monkeypatch.setattr(xcelium_ams, "xrun_env", lambda _xrun: {})

    result = execute_xcelium_ams_cell(
        plan_xcelium_ams_cell(contract, project=project),
        artifacts=_run_artifacts(tmp_path),
        xrun=xrun,
    )

    assert result.passed
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_evidence"] == ["native_log"]
    assert summary["product_qualification_conclusion"] is False


def test_xcelium_ams_execution_reports_missing_success_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _patch_native_resolution(monkeypatch, root=tmp_path, circuit=circuit)
    xrun = _write(tmp_path / "tools/xrun", "#!/bin/sh\nexit 99\n")
    xrun.chmod(0o755)

    def capture(command, *, before_spawn, **_kwargs):
        before_spawn()
        return subprocess.CompletedProcess(command, 0, "FAIL transaction\n", "")

    monkeypatch.setattr(xcelium_ams, "run_process_group_capture", capture)
    monkeypatch.setattr(xcelium_ams, "xrun_env", lambda _xrun: {})

    project = Project.from_project_root(tmp_path)
    result = execute_xcelium_ams_cell(
        plan_xcelium_ams_cell(contract, project=project),
        artifacts=_run_artifacts(tmp_path),
        xrun=xrun,
    )

    assert not result.passed
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_seen"] is False
