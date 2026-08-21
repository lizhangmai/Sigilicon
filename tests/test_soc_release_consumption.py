from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sigilicon.artifacts import file_sha256
from sigilicon.workflows.soc import check_soc, resolve_soc_fileset


def _write_release_fixture(artifact_root: Path) -> tuple[str, str]:
    release_id = "development-fixture"
    relative_root = Path("ip/fixture-ip") / release_id
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
            "sha256": file_sha256(path),
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
        "source_fingerprint": "b" * 64,
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


def _write_soc_fixture(project_root: Path, release_id: str, manifest: str) -> Path:
    product = project_root / "soc/demo"
    (product / "rtl").mkdir(parents=True)
    (product / "variants").mkdir()
    (product / "rtl/top.sv").write_text(
        "module top(input logic clk);\nendmodule\n", encoding="utf-8"
    )
    (product / "rtl/simulation.f").write_text(
        "soc/demo/rtl/top.sv\n", encoding="utf-8"
    )
    (product / "variants/default.toml").write_text(
        """schema = 1
contract_kind = "soc-variant"
path_scope = "variant"
owner = "soc"

[integration]
variant = "default"
default_fileset = "simulation"

[filesets.simulation]
filelist = "soc/demo/rtl/simulation.f"
required_capability = "simulation"

[filesets.simulation.ip_roles]
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
    (product / "ip.lock.toml").write_text(
        f"""schema = 1
contract_kind = "soc-release-lock"
path_scope = "product"
owner = "soc"

soc = "demo"

[[ip]]
name = "fixture-ip"
release_id = "{release_id}"
manifest = "{manifest}"
maturity = "development"
""",
        encoding="utf-8",
    )
    contract = product / "soc.toml"
    contract.write_text(
        """schema = 1
contract_kind = "soc-contract"
path_scope = "product"
owner = "soc"

name = "demo"
lock = "ip.lock.toml"
required_ip_roles = [
  "transaction_model",
  "integration_adapter",
  "physical_blackbox",
]

[[ip]]
name = "fixture-ip"
export = "macro"
required_maturity = "development"
logical_interface = "fixture_model:transaction-1-port"
physical_interface = "fixture_macro:oa-1-pin"

[ip.role_modules]
transaction_model = "fixture_model"
integration_adapter = "fixture_shell"
physical_blackbox = "fixture_macro"

[variants]
default = "variants/default.toml"
""",
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


def test_soc_check_keeps_paths_public_and_resolves_them_only_for_execution(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_soc_fixture(project_root, release_id, manifest)

    result = check_soc(
        contract,
        project_root=project_root,
        artifact_root=artifact_root,
        variant_name="default",
    )

    assert result["schema"] == 1
    assert result["contract_kind"] == "soc-check"
    assert result["owner"] == "soc"
    assert result["passed"] is True
    assert _absolute_strings(result) == []
    assert "sources" not in result
    assert result["product_sources"] == ["soc/demo/rtl/top.sv"]
    assert result["release_sources"] == [
        f"ip/fixture-ip/{release_id}/rtl/fixture_model.sv"
    ]
    assert result["ip_releases"][0]["manifest"] == manifest

    resolved = resolve_soc_fileset(
        contract,
        project_root=project_root,
        artifact_root=artifact_root,
        variant_name="default",
    )

    assert resolved == (
        (project_root / "soc/demo/rtl/top.sv").resolve(),
        (artifact_root / result["release_sources"][0]).resolve(),
    )
    assert all(path.is_absolute() and path.is_file() for path in resolved)


def test_soc_check_rejects_a_lock_outside_the_product_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_soc_fixture(project_root, release_id, manifest)
    external_lock = tmp_path / "external.lock.toml"
    external_lock.write_text(
        (contract.parent / "ip.lock.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inside the project root"):
        check_soc(
            contract,
            project_root=project_root,
            artifact_root=artifact_root,
            variant_name="default",
            lock_path=external_lock,
        )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("lock-identity", "lock identity"),
        ("unavailable", "unavailable for simulation"),
        ("stale-role", "digest drifted"),
    ),
)
def test_soc_check_rejects_invalid_locked_release_state(
    tmp_path: Path, case: str, message: str
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_soc_fixture(project_root, release_id, manifest)
    manifest_path = artifact_root / manifest

    if case == "lock-identity":
        lock_path = contract.parent / "ip.lock.toml"
        lock_path.write_text(
            lock_path.read_text(encoding="utf-8").replace(
                f'release_id = "{release_id}"',
                'release_id = "wrong-release"',
            ),
            encoding="utf-8",
        )
    elif case == "unavailable":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["exports"][0]["availability"]["simulation"] = False
        manifest_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    elif case == "stale-role":
        role_path = manifest_path.parent / "rtl/fixture_model.sv"
        role_path.write_text(
            role_path.read_text(encoding="utf-8") + "// stale\n",
            encoding="utf-8",
        )
    else:  # pragma: no cover - the parametrization is closed above
        raise AssertionError(case)

    with pytest.raises(RuntimeError, match=message):
        check_soc(
            contract,
            project_root=project_root,
            artifact_root=artifact_root,
            variant_name="default",
        )
