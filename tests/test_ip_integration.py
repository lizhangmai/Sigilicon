from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sigilicon.cli.main import main as sigilicon_cli_main
from sigilicon.workflows.ip_integration import (
    check_ip_integration,
    plan_ip_integration,
    resolve_ip_integration_fileset,
)


def _fixture_architecture_validator(raw: dict[str, Any]) -> dict[str, Any]:
    return {"variant": raw["integration"]["variant"], "validated": True}


def _write_release_fixture(artifact_root: Path) -> tuple[str, str]:
    release_id = "development-fixture"
    relative_root = Path("exports/fixture-ip/package") / release_id
    release_root = artifact_root / relative_root
    release_root.mkdir(parents=True)
    files = {
        "interfaces/interface.toml": """[physical_macro]
module = "fixture_macro"

[transaction_boundary]
module = "fixture_model"
ams_wrapper_module = "fixture_shell"
ports = [{ name = "clk", direction = "input", width = 1 }]
""",
        "interfaces/oa_ports.toml": """[ports]
order = ["P"]

[ports.directions]
P = "input"
""",
        "rtl/fixture_macro.sv": (
            "module fixture_macro(input logic P);\nendmodule\n"
        ),
        "rtl/fixture_model.sv": (
            "module fixture_model(input logic clk);\nendmodule\n"
        ),
        "rtl/fixture_shell.sv": """module fixture_shell(
  input logic clk,
  input logic P
);
  fixture_macro macro_i (.P(P));
endmodule
""",
        "circuit/fixture_macro.scs": (
            "subckt fixture_macro P\nends fixture_macro\n"
        ),
    }
    role_metadata = {
        "interface_contract": ("interfaces/interface.toml", None),
        "oa_port_contract": ("interfaces/oa_ports.toml", None),
        "physical_blackbox": ("rtl/fixture_macro.sv", "fixture_macro"),
        "transaction_model": ("rtl/fixture_model.sv", "fixture_model"),
        "integration_adapter": ("rtl/fixture_shell.sv", "fixture_shell"),
        "circuit_netlist": ("circuit/fixture_macro.scs", None),
    }
    for relative, content in files.items():
        path = release_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    views = []
    for role, (relative, module) in role_metadata.items():
        path = release_root / relative
        view: dict[str, Any] = {
            "export": "macro",
            "role": role,
            "path": relative,
            "format": "systemverilog" if path.suffix == ".sv" else path.suffix[1:],
            "size": path.stat().st_size,
        }
        if module is not None:
            view["module"] = module
        views.append(view)
    manifest = {
        "schema": 1,
        "contract_kind": "ip-release-manifest",
        "release_kind": "source-package",
        "owner": "fixture",
        "ip_name": "fixture-ip",
        "release_id": release_id,
        "source_commit": "a" * 40,
        "source": {"commit": "a" * 40, "dirty": False},
        "exports": [
            {
                "name": "macro",
                "oa": {
                    "library": "fixture",
                    "cell": "fixture_macro",
                    "schematic_view": "schematic",
                    "layout_view": "layout",
                },
                "interface": {
                    "physical": "fixture_macro:oa-1-pin",
                    "logical": "fixture_model:transaction-1-port",
                },
                "maturity": {"required_roles": list(role_metadata)},
                "availability": {
                    "simulation": True,
                    "synthesis": False,
                    "physical_implementation": False,
                },
            }
        ],
        "views": views,
        "maturity": {
            "level": "development",
            "checks": [{"name": "fixture-interface", "passed": True}],
            "missing_items": [],
        },
        "provenance": {"working_tree_dirty": False},
    }
    (release_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return release_id, (relative_root / "manifest.json").as_posix()


def _write_ip_fixture(project_root: Path, release_id: str, manifest: str) -> Path:
    owner_root = project_root / "ip/demo"
    dependency_root = project_root / "ip/fixture"
    (owner_root / "configs/variants").mkdir(parents=True)
    (owner_root / "rtl").mkdir()
    (dependency_root / "configs").mkdir(parents=True)
    (dependency_root / "configs/ip.toml").write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture"

name = "fixture-ip"
kind = "rtl-ip"

[filesets]
source = ["ip/fixture/configs/ip.toml"]
''',
        encoding="utf-8",
    )
    (owner_root / "rtl/top.sv").write_text(
        "module top(input logic clk);\nendmodule\n", encoding="utf-8"
    )
    (owner_root / "rtl/simulation.f").write_text(
        "ip/demo/rtl/top.sv\n", encoding="utf-8"
    )
    (owner_root / "configs/variants/default.toml").write_text(
        """schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "demo"

[integration]
variant = "default"
default_fileset = "simulation"

[architecture_validation]
validator = "test_ip_integration:_fixture_architecture_validator"

[filesets.simulation]
filelist = "ip/demo/rtl/simulation.f"
required_capability = "simulation"

[filesets.simulation.dependency_roles]
fixture-ip = ["transaction_model"]

[physical_binding]
dependency = "fixture-ip"
transaction_module = "fixture_model"
physical_shell_module = "fixture_shell"
adapter_module = "fixture_adapter"
raw_macro_module = "fixture_macro"
status = "blocked"
blockers = ["implementation_release_missing"]
""",
        encoding="utf-8",
    )
    (owner_root / "configs/dependency.lock.toml").write_text(
        f"""schema = 1
contract_kind = "ip-dependency-lock"
path_scope = "owner"
owner = "demo"

ip = "demo"

[[dependency]]
name = "fixture-ip"
release_id = "{release_id}"
manifest = "{manifest}"
maturity = "development"
""",
        encoding="utf-8",
    )
    contract = owner_root / "configs/ip.toml"
    contract.write_text(
        """schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "demo"

name = "demo"
kind = "composite-ip"
dependency_lock = "ip/demo/configs/dependency.lock.toml"

[[component]]
name = "fixture-ip"
contract = "ip/fixture/configs/ip.toml"

[component.release]
export = "macro"
required_maturity = "development"
logical_interface = "fixture_model:transaction-1-port"
physical_interface = "fixture_macro:oa-1-pin"
roles = ["transaction_model", "integration_adapter", "physical_blackbox"]

[component.release.role_modules]
transaction_model = "fixture_model"
integration_adapter = "fixture_shell"
physical_blackbox = "fixture_macro"

[variants]
default = "ip/demo/configs/variants/default.toml"

[filesets]
rtl = [
  "ip/demo/rtl/top.sv",
  "ip/demo/rtl/simulation.f",
  "ip/demo/configs/variants/default.toml",
]
""",
        encoding="utf-8",
    )
    (project_root / "configs/platform").mkdir(parents=True)
    (project_root / "configs/platform/catalog.toml").write_text(
        '''schema = 1
contract_kind = "platform-catalog"
path_scope = "repository"
owner = "repository"

[platforms]
''',
        encoding="utf-8",
    )
    (project_root / "ip/catalog.toml").write_text(
        '''schema = 1
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "repository"

[targets]

[components.demo]
contract = "ip/demo/configs/ip.toml"
root = "ip/demo"

[components.fixture-ip]
contract = "ip/fixture/configs/ip.toml"
root = "ip/fixture"
''',
        encoding="utf-8",
    )
    (project_root / "sigilicon.toml").write_text(
        '''schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "repository"

[catalogs]
ip = "ip/catalog.toml"
platform = "configs/platform/catalog.toml"

[paths]
project_root = "."
workspace_root = "workspace"
artifact_root = "../artifacts"
''',
        encoding="utf-8",
    )
    return contract


def _absolute_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _absolute_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _absolute_strings(child)]
    if isinstance(value, str) and Path(value).is_absolute():
        return [value]
    return []


def _write_source_component_fixture(project_root: Path) -> Path:
    dependency = project_root / "ip/leaf"
    owner = project_root / "ip/composite"
    (dependency / "rtl").mkdir(parents=True)
    (dependency / "configs").mkdir()
    (owner / "rtl").mkdir(parents=True)
    (owner / "configs/variants").mkdir(parents=True)
    (dependency / "rtl/leaf.sv").write_text(
        "module leaf(input logic clk); endmodule\n", encoding="utf-8"
    )
    (dependency / "configs/ip.toml").write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "leaf"

name = "leaf"
kind = "rtl-ip"

[filesets]
rtl = ["ip/leaf/rtl/leaf.sv"]
''',
        encoding="utf-8",
    )
    (owner / "rtl/top.sv").write_text(
        "module top(input logic clk); leaf child(clk); endmodule\n",
        encoding="utf-8",
    )
    (owner / "rtl/simulation.f").write_text(
        "ip/leaf/rtl/leaf.sv\nip/composite/rtl/top.sv\n",
        encoding="utf-8",
    )
    (owner / "configs/variants/default.toml").write_text(
        '''schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "composite"

[integration]
variant = "default"
default_fileset = "simulation"

[filesets.simulation]
filelist = "ip/composite/rtl/simulation.f"
required_capability = "simulation"

[filesets.simulation.source_filesets]
leaf = "rtl"
''',
        encoding="utf-8",
    )
    contract = owner / "configs/ip.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "composite"

name = "composite"
kind = "composite-ip"

[variants]
default = "ip/composite/configs/variants/default.toml"

[filesets]
rtl = [
  "ip/composite/rtl/top.sv",
  "ip/composite/rtl/simulation.f",
  "ip/composite/configs/variants/default.toml",
]

[[component]]
name = "leaf"
contract = "ip/leaf/configs/ip.toml"
''',
        encoding="utf-8",
    )
    return contract


def test_ip_integration_check_keeps_paths_public_and_resolves_only_for_execution(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)

    result = check_ip_integration(
        contract,
        project_root=project_root,
        artifact_root=artifact_root,
        variant_name="default",
    )

    assert result["schema"] == 1
    assert result["contract_kind"] == "ip-integration-check"
    assert result["owner"] == "demo"
    assert result["passed"] is True
    assert result["architecture"] == {"variant": "default", "validated": True}
    assert _absolute_strings(result) == []
    assert "sources" not in result
    assert result["source_files"] == ["ip/demo/rtl/top.sv"]
    assert result["release_sources"] == [
        f"exports/fixture-ip/package/{release_id}/rtl/fixture_model.sv"
    ]
    assert result["dependency_releases"][0]["manifest"] == manifest

    resolved = resolve_ip_integration_fileset(
        contract,
        project_root=project_root,
        artifact_root=artifact_root,
        variant_name="default",
    )

    assert resolved == (
        (project_root / "ip/demo/rtl/top.sv").resolve(),
        (artifact_root / result["release_sources"][0]).resolve(),
    )
    assert all(path.is_absolute() and path.is_file() for path in resolved)


def test_ip_integration_is_exposed_only_under_the_ip_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    _write_ip_fixture(project_root, release_id, manifest)
    monkeypatch.chdir(project_root)

    result = sigilicon_cli_main(
        [
            "ip",
            "integration",
            "check",
            "demo",
            "--variant",
            "default",
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_kind"] == "ip-integration-check"
    assert payload["ip"] == "demo"
    assert _absolute_strings(payload) == []


def test_source_level_child_ip_is_selected_by_fileset_without_a_release_lock(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    contract = _write_source_component_fixture(project_root)

    plan = plan_ip_integration(
        contract,
        project_root=project_root,
        artifact_root=tmp_path / "artifacts",
    )

    result = check_ip_integration(
        contract,
        project_root=project_root,
        artifact_root=tmp_path / "artifacts",
        variant_name="default",
    )

    assert plan["source_only"] is True
    assert plan["dependencies"] == [
        {
            "name": "leaf",
            "component": "ip/leaf/configs/ip.toml",
        }
    ]
    assert result["dependency_lock"] is None
    assert result["dependency_releases"] == []
    assert result["source_files"] == [
        "ip/leaf/rtl/leaf.sv",
        "ip/composite/rtl/top.sv",
    ]


def test_declaring_release_capability_does_not_implicitly_consume_it(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'dependency_lock = "ip/demo/configs/dependency.lock.toml"\n',
            "",
        ),
        encoding="utf-8",
    )
    variant = contract.parent / "variants/default.toml"
    variant.write_text(
        variant.read_text(encoding="utf-8").replace(
            '\n[filesets.simulation.dependency_roles]\n'
            'fixture-ip = ["transaction_model"]\n',
            "",
        ),
        encoding="utf-8",
    )

    result = check_ip_integration(
        contract,
        project_root=project_root,
        artifact_root=artifact_root,
        variant_name="default",
    )

    assert result["dependency_lock"] is None
    assert result["dependency_releases"] == []


def test_ip_integration_rejects_a_lock_outside_the_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    external_lock = tmp_path / "external.lock.toml"
    external_lock.write_text(
        (contract.parent / "dependency.lock.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inside the project root"):
        check_ip_integration(
            contract,
            project_root=project_root,
            artifact_root=artifact_root,
            variant_name="default",
            lock_path=external_lock,
        )


def test_ip_integration_keeps_physical_readiness_separate_from_source_planning(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    variant = contract.parent / "variants/default.toml"
    variant.write_text(
        variant.read_text(encoding="utf-8").replace(
            'required_capability = "simulation"',
            'required_capability = "synthesis"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="physical binding is blocked"):
        check_ip_integration(
            contract,
            project_root=project_root,
            artifact_root=artifact_root,
            variant_name="default",
        )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("lock-identity", "lock identity"),
        ("lock-maturity", "lock maturity"),
        ("unavailable", "unavailable for simulation"),
        ("dirty-source", "dirty source"),
        ("interface-drift", "interface identities disagree"),
        ("failed-maturity-check", "maturity checks are incomplete"),
        ("module-drift", "module disagrees"),
        ("missing-manifest", "manifest"),
        ("stale-role", "size drifted"),
    ),
)
def test_ip_integration_rejects_invalid_locked_release_state(
    tmp_path: Path, case: str, message: str
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    manifest_path = artifact_root / manifest

    if case == "lock-identity":
        lock_path = contract.parent / "dependency.lock.toml"
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8").replace(
                f'release_id = "{release_id}"',
                'release_id = "wrong-release"',
            ),
            encoding="utf-8",
        )
    elif case == "lock-maturity":
        lock_path = contract.parent / "dependency.lock.toml"
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8").replace(
                'maturity = "development"',
                'maturity = "implementation"',
            ),
            encoding="utf-8",
        )
    elif case == "unavailable":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["exports"][0]["availability"]["simulation"] = False
        manifest_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "dirty-source":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["source"]["dirty"] = True
        payload["provenance"]["working_tree_dirty"] = True
        manifest_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "interface-drift":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["exports"][0]["interface"]["logical"] = "wrong:interface"
        manifest_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "failed-maturity-check":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["maturity"]["checks"][0]["passed"] = False
        manifest_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "module-drift":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        next(
            view for view in payload["views"] if view["role"] == "transaction_model"
        )["module"] = "wrong_module"
        manifest_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "missing-manifest":
        manifest_path.unlink()
    elif case == "stale-role":
        role_path = manifest_path.parent / "rtl/fixture_model.sv"
        role_path.write_text(
            role_path.read_text(encoding="utf-8") + "// stale\n",
            encoding="utf-8",
        )
    else:  # pragma: no cover - the parametrization is closed above
        raise AssertionError(case)

    with pytest.raises((RuntimeError, FileNotFoundError), match=message):
        check_ip_integration(
            contract,
            project_root=project_root,
            artifact_root=artifact_root,
            variant_name="default",
        )
