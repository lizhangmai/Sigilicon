from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.project import Project
from sigilicon.execution._model import Resources
from sigilicon.external_tools import ProcessResult
from sigilicon.workflows import xcelium_ams
from sigilicon.execution._workspace import ExecutionWorkspace
from sigilicon.workflows.xcelium_ams import (
    execute_xcelium_ams_cell,
    plan_xcelium_ams_cell,
)

from conftest import write_component_owner, write_test_platform


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
transient_stop = "1u"
ie_voltage = 0.9

[ams.circuit]
kind = "ip-release"
contract = "../../../component.toml"
variant = "no-recovery"
fileset = "ams"
dependency = "native-provider"
role = "circuit_netlist"
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
    relative = circuit.relative_to(root / "artifacts").as_posix()
    monkeypatch.setattr(
        xcelium_ams,
        "_locked_native_release",
        lambda _spec, _selection, _resources: (
            "NATIVE_TOP",
            circuit,
            {
                "schema": 1,
                "contract_kind": "locked-release-selection",
                "passed": True,
                "dependency_releases": [
                    {
                        "name": "native-provider",
                        "export": "native-top",
                        "release_id": "development-123456789abc",
                        "store": "native-provider",
                        "manifest_sha256": "1" * 64,
                        "roles": {"circuit_netlist": relative},
                    }
                ],
            },
            {circuit: hashlib.sha256(circuit.read_bytes()).hexdigest()},
        ),
    )


def _configure_locked_native_release(root: Path, circuit: Path) -> Path:
    _write(
        root / "ip/native-provider/configs/release.toml",
        '''schema = 2
contract_kind = "ip-release"
path_scope = "owner"
owner = "native-provider"
''',
    )
    write_component_owner(
        root,
        "native-provider",
        filesets={
            "release": ("ip/native-provider/configs/release.toml",),
        },
    )
    component = root / "ip/demo/component.toml"
    component.write_text(
        component.read_text(encoding="utf-8")
        .replace('kind = "rtl-ip"', 'kind = "composite-ip"')
        .replace(
            "[sources]\n",
            'dependency_lock = "dependency_lock"\n\n[sources]\n'
            'dependency_lock = "ip/demo/configs/dependency.lock.toml"\n'
            'no_recovery_variant = "ip/demo/configs/variants/no_recovery.toml"\n',
        )
        + '''
[[component]]
name = "native-provider"
contract = "ip/native-provider/component.toml"

[component.release]
export = "native-top"
required_maturity = "development"
roles = ["circuit_netlist"]

[variants]
no-recovery = "no_recovery_variant"
''',
        encoding="utf-8",
    )
    _write(
        root / "ip/demo/configs/variants/no_recovery.toml",
        '''schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "demo"

[integration]
variant = "no-recovery"
component_contract = "ip/demo/component.toml"
default_fileset = "ams"

[filesets.ams]
filelist = "ip/demo/configs/ams.f"
required_capability = "simulation"

[filesets.ams.dependency_roles]
native-provider = ["circuit_netlist"]
''',
    )
    _write(
        root / "ip/demo/configs/ams.f",
        "ip/demo/rtl/native_adapter.sv\n",
    )
    release_root = circuit.parent
    interface = _write(
        release_root / "interface.toml",
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "native-provider"

[physical]
library = "native"
cell = "NATIVE_TOP"
port_count = 2
canonical_port_contract = "ip/native-provider/configs/ports.toml"

[behavior]
result = "native response"

[supplies]
domains = []
''',
    )
    ports = _write(
        release_root / "ports.toml",
        '''[ports]
order = ["A", "VSS"]

[ports.directions]
A = "input"
VSS = "inout"
''',
    )
    manifest = release_root / "manifest.json"
    payload = {
        "schema": 2,
        "contract_kind": "ip-release-manifest",
        "release_kind": "source-package",
        "ip_name": "native-provider",
        "owner": "native-provider",
        "release_id": "development-" + "a" * 40,
        "source_commit": "a" * 40,
        "exports": [
            {
                "name": "native-top",
                "oa": {
                    "library": "native",
                    "cell": "NATIVE_TOP",
                    "schematic_view": "schematic",
                    "layout_view": "layout",
                },
                "interface": {
                    "kind": "oa-native",
                    "contract": "ip/native-provider/configs/interface.toml",
                },
                "maturity": {
                    "required_roles": [
                        "interface_contract",
                        "oa_port_contract",
                        "circuit_netlist",
                    ]
                },
                "availability": {"simulation": True},
            }
        ],
        "views": [
            {
                "export": "native-top",
                "role": "interface_contract",
                "path": interface.name,
                "source": "ip/native-provider/configs/interface.toml",
                "format": "toml",
                "size": interface.stat().st_size,
                "sha256": hashlib.sha256(interface.read_bytes()).hexdigest(),
            },
            {
                "export": "native-top",
                "role": "oa_port_contract",
                "path": ports.name,
                "source": "ip/native-provider/configs/ports.toml",
                "format": "toml",
                "size": ports.stat().st_size,
                "sha256": hashlib.sha256(ports.read_bytes()).hexdigest(),
            },
            {
                "export": "native-top",
                "role": "circuit_netlist",
                "path": circuit.name,
                "format": "spectre-source",
                "composition": "reachable-spectre-hierarchy",
                "subcircuits": ["NATIVE_TOP"],
                "primitive_masters": [],
                "size": circuit.stat().st_size,
                "sha256": hashlib.sha256(circuit.read_bytes()).hexdigest(),
            }
        ],
        "maturity": {
            "level": "development",
            "checks": [{"name": "fixture", "passed": True}],
        },
        "provenance": {"producer": "ip/native-provider"},
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    object_id = f"sha256-{digest}"
    published = (
        root
        / "artifacts/release-store/native-provider/objects"
        / object_id
    )
    for source in (circuit, interface, ports, manifest):
        _write(published / source.name, source.read_text(encoding="utf-8"))
    _write(
        root / "ip/demo/configs/dependency.lock.toml",
        f'''schema = 3
contract_kind = "ip-dependency-lock"
path_scope = "owner"
owner = "demo"
ip = "demo"

[[dependency]]
name = "native-provider"
release_id = "development-{'a' * 40}"
store = "native-provider"
source_commit = "{'a' * 40}"
manifest_sha256 = "{digest}"
maturity = "development"
''',
    )
    return published / circuit.name


def test_xcelium_ams_plan_resolves_locked_circuit_and_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _patch_native_resolution(monkeypatch, root=tmp_path, circuit=circuit)

    plan = plan_xcelium_ams_cell(
        contract,
        project=Project.open(tmp_path),
        resources=Project.open(tmp_path).resources(),
    )

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
    serialized = json.dumps(plan.as_dict(), sort_keys=True)
    assert str(tmp_path) not in serialized
    assert circuit.read_text(encoding="utf-8") not in serialized


def test_xcelium_ams_plan_accepts_a_project_owned_standalone_circuit(
    tmp_path: Path,
) -> None:
    contract, _release_circuit = _ams_project(tmp_path)
    circuit = _write(
        tmp_path / "ip/demo/design/analog_top.scs",
        "simulator lang=spectre\nsubckt ANALOG_TOP A VSS\nends ANALOG_TOP\n",
    )
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            '''kind = "ip-release"
contract = "../../../component.toml"
variant = "no-recovery"
fileset = "ams"
dependency = "native-provider"
role = "circuit_netlist"''',
            '''kind = "source"
path = "../../../design/analog_top.scs"
cell = "ANALOG_TOP"''',
        ),
        encoding="utf-8",
    )

    plan = plan_xcelium_ams_cell(
        contract,
        project=Project.open(tmp_path),
        resources=Project.open(tmp_path).resources(),
    )

    assert plan.native_cell == "ANALOG_TOP"
    assert plan.circuit_netlist == circuit.resolve()
    assert plan.integration_check["contract_kind"] == "source-circuit-selection"
    assert plan.circuit_sha256 == hashlib.sha256(circuit.read_bytes()).hexdigest()
    assert circuit not in plan.resource_identities
    assert set(plan.resource_identities) == set(plan.model_set.files)
    assert all(
        identity.startswith("pdk:testpdk:simulation/nominal/")
        for identity in plan.resource_identities.values()
    )


def test_xcelium_ams_rejects_unknown_dependency_lock_fields(
    tmp_path: Path,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _configure_locked_native_release(tmp_path, circuit)
    lock = tmp_path / "ip/demo/configs/dependency.lock.toml"
    lock.write_text(
        lock.read_text(encoding="utf-8") + 'legacy_manifest = "path"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fields must be exactly"):
        plan_xcelium_ams_cell(
            contract,
            project=Project.open(tmp_path),
            resources=Project.open(tmp_path).resources(),
        )


def test_xcelium_ams_rejects_same_size_release_tampering(
    tmp_path: Path,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    circuit = _configure_locked_native_release(tmp_path, circuit)
    payload = bytearray(circuit.read_bytes())
    payload[0] ^= 1
    circuit.write_bytes(payload)

    with pytest.raises(ValueError, match="release package"):
        plan_xcelium_ams_cell(
            contract,
            project=Project.open(tmp_path),
            resources=Project.open(tmp_path).resources(),
        )


def test_xcelium_ams_rejects_a_schema_two_package_without_exports(
    tmp_path: Path,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    circuit = _configure_locked_native_release(tmp_path, circuit)
    manifest = circuit.parent / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("exports")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    new_object = f"sha256-{digest}"
    manifest.parent.rename(manifest.parent.parent / new_object)
    lock = tmp_path / "ip/demo/configs/dependency.lock.toml"
    source = lock.read_text(encoding="utf-8")
    marker = 'manifest_sha256 = "'
    start = source.index(marker) + len(marker)
    end = source.index('"', start)
    lock.write_text(source[:start] + digest + source[end:], encoding="utf-8")

    with pytest.raises(ValueError, match="release package"):
        plan_xcelium_ams_cell(
            contract,
            project=Project.open(tmp_path),
            resources=Project.open(tmp_path).resources(),
        )


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

    with pytest.raises(ValueError, match="AMS circuit selection"):
        plan_xcelium_ams_cell(
            contract,
            project=Project.open(tmp_path),
            resources=Project.open(tmp_path).resources(),
        )


def test_xcelium_ams_execution_stages_inputs_and_records_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, circuit = _ams_project(tmp_path)
    _patch_native_resolution(monkeypatch, root=tmp_path, circuit=circuit)
    project = Project.open(tmp_path)
    xrun = _write(
        tmp_path / "tools/xcelium/tools/bin/xrun",
        "#!/bin/sh\nexit 99\n",
    )
    xrun.chmod(0o755)
    spectre = _write(
        tmp_path / "tools/spectre/tools/bin/spectre",
        "#!/bin/sh\nexit 99\n",
    )
    spectre.chmod(0o755)

    def capture(request):
        assert str(request.cwd).startswith("/proc/") and "/fd/" in str(request.cwd)
        assert len(request.pass_fds) == 2
        request.before_spawn()
        spectre_index = request.argv.index("-spectre_path") + 1
        assert request.argv[spectre_index] == str(spectre.parent)
        control = Path(request.argv[-1]).read_text(encoding="utf-8")
        assert "/inputs/release/circuit.scs" in control
        assert "/inputs/pdk/model.scs" in control
        assert str(circuit) not in control
        (request.cwd / "xrun.log").write_text(
            "TB_DEMO_AMS_SUMMARY failures=0\n",
            encoding="utf-8",
        )
        return ProcessResult(returncode=0, stdout="", stderr="")

    result = execute_xcelium_ams_cell(
        plan_xcelium_ams_cell(
            contract,
            project=project,
            resources=Project.open(tmp_path).resources(),
        ),
        artifacts=_run_artifacts(tmp_path),
        resources=Resources(
            tools={
                "cadence.xrun": str(xrun),
                "cadence.spectre": str(spectre),
            }
        ),
        process=SimpleNamespace(run=capture),
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
    xrun = _write(
        tmp_path / "tools/xcelium/tools/bin/xrun",
        "#!/bin/sh\nexit 99\n",
    )
    xrun.chmod(0o755)
    spectre = _write(
        tmp_path / "tools/spectre/tools/bin/spectre",
        "#!/bin/sh\nexit 99\n",
    )
    spectre.chmod(0o755)

    def capture(request):
        request.before_spawn()
        return ProcessResult(
            returncode=0,
            stdout="FAIL transaction\n",
            stderr="",
        )

    project = Project.open(tmp_path)
    result = execute_xcelium_ams_cell(
        plan_xcelium_ams_cell(
            contract,
            project=project,
            resources=Project.open(tmp_path).resources(),
        ),
        artifacts=_run_artifacts(tmp_path),
        resources=Resources(
            tools={
                "cadence.xrun": str(xrun),
                "cadence.spectre": str(spectre),
            }
        ),
        process=SimpleNamespace(run=capture),
    )

    assert not result.passed
    summary = json.loads(result.run_summary.read_text(encoding="utf-8"))
    assert summary["success_marker_seen"] is False
