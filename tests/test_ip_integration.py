from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib
from types import SimpleNamespace
from typing import Any

import pytest

import sigilicon.workflows.ip_integration as ip_integration
from sigilicon.domain.ip_integration import (
    LockedIpRelease,
    OaNativePhysicalBinding,
    load_ip_integration_contract,
)
from sigilicon.project import Project
from sigilicon.workflows.ip_integration import (
    check_ip_integration,
    plan_ip_integration,
    resolve_locked_ip_release,
    resolve_ip_integration_fileset,
)

from conftest import write_project_context


def _write_release_fixture(artifact_root: Path) -> tuple[str, str]:
    release_id = "development-fixture"
    relative_root = Path("staging/fixture-ip") / release_id
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
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if module is not None:
            view["module"] = module
        views.append(view)
    manifest = {
        "schema": 2,
        "contract_kind": "ip-release-manifest",
        "release_kind": "source-package",
        "ip_name": "fixture-ip",
        "release_id": release_id,
        "source_commit": "a" * 40,
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
                    "kind": "oa-mixed-signal",
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
        "provenance": {
            "producer": "ip/fixture",
        },
    }
    (release_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    digest = hashlib.sha256((release_root / "manifest.json").read_bytes()).hexdigest()
    relative_root = Path("release-store/fixture/objects") / f"sha256-{digest}"
    published = artifact_root / relative_root
    published.parent.mkdir(parents=True)
    release_root.rename(published)
    return release_id, (relative_root / "manifest.json").as_posix()


def _write_rtl_release_fixture(artifact_root: Path) -> tuple[str, str]:
    release_id = "development-rtl-fixture"
    relative_root = Path("staging/fixture-ip") / release_id
    release_root = artifact_root / relative_root
    release_root.mkdir(parents=True)
    interface = release_root / "interfaces/interface.toml"
    source = release_root / "rtl/fixture_rtl.sv"
    interface.parent.mkdir()
    source.parent.mkdir()
    interface.write_text(
        '''[module]
name = "fixture_rtl"
source = "ip/fixture/rtl/fixture_rtl.sv"
ports = [{ name = "clk", direction = "input", width = 1 }]
''',
        encoding="utf-8",
    )
    source.write_text(
        "module fixture_rtl(input logic clk);\nendmodule\n",
        encoding="utf-8",
    )
    views = [
        {
            "export": "rtl",
            "role": "interface_contract",
            "path": "interfaces/interface.toml",
            "source": "ip/fixture/configs/interface.toml",
            "format": "toml",
            "size": interface.stat().st_size,
            "sha256": hashlib.sha256(interface.read_bytes()).hexdigest(),
        },
        {
            "export": "rtl",
            "role": "rtl_source",
            "path": "rtl/fixture_rtl.sv",
            "source": "ip/fixture/rtl/fixture_rtl.sv",
            "format": "systemverilog",
            "module": "fixture_rtl",
            "size": source.stat().st_size,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
    ]
    manifest = {
        "schema": 2,
        "contract_kind": "ip-release-manifest",
        "release_kind": "source-package",
        "ip_name": "fixture-ip",
        "release_id": release_id,
        "source_commit": "a" * 40,
        "exports": [
            {
                "name": "rtl",
                "interface": {
                    "kind": "rtl",
                    "contract": "ip/fixture/configs/interface.toml",
                    "module": "fixture_rtl",
                    "source_role": "rtl_source",
                },
                "maturity": {
                    "required_roles": ["interface_contract", "rtl_source"]
                },
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
            "checks": [{"name": "rtl-interface", "passed": True}],
            "missing_items": [],
        },
        "provenance": {
            "producer": "ip/fixture",
        },
    }
    (release_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    digest = hashlib.sha256((release_root / "manifest.json").read_bytes()).hexdigest()
    relative_root = Path("release-store/fixture/objects") / f"sha256-{digest}"
    published = artifact_root / relative_root
    published.parent.mkdir(parents=True)
    release_root.rename(published)
    return release_id, (relative_root / "manifest.json").as_posix()


def test_locked_release_rejects_a_symlinked_manifest_parent(tmp_path: Path) -> None:
    real_artifacts = tmp_path / "real-artifacts"
    release_id, relative_manifest = _write_release_fixture(real_artifacts)
    manifest = real_artifacts / relative_manifest
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (artifact_root / "release-store").symlink_to(
        real_artifacts / "release-store",
        target_is_directory=True,
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    pinned = LockedIpRelease(
        name="fixture-ip",
        release_id=release_id,
        store="fixture",
        maturity="development",
        source_commit="a" * 40,
        manifest_sha256=digest,
    )

    with pytest.raises((FileNotFoundError, RuntimeError), match="symlink"):
        resolve_locked_ip_release(
            release_store_root=artifact_root / "release-store",
            pinned=pinned,
        )


def _select_rtl_dependency(contract: Path) -> None:
    source = contract.read_text(encoding="utf-8")
    source = source.replace(
        '''[component.release]
export = "macro"
required_maturity = "development"
roles = ["transaction_model", "integration_adapter", "physical_blackbox"]
''',
        '''[component.release]
export = "rtl"
required_maturity = "development"
roles = ["rtl_source"]
''',
    )
    contract.write_text(source, encoding="utf-8")
    variant = contract.parent / "variants/default.toml"
    variant_source = variant.read_text(encoding="utf-8")
    variant_source = variant_source.replace(
        'fixture-ip = ["transaction_model"]',
        'fixture-ip = ["rtl_source"]',
    )
    physical = variant_source.index("[physical_binding]")
    variant.write_text(variant_source[:physical], encoding="utf-8")


def _select_native_oa_dependency(
    contract: Path,
    *,
    artifact_root: Path,
    manifest: str,
) -> Path:
    source = contract.read_text(encoding="utf-8")
    source = source.replace(
        '''[component.release]
export = "macro"
required_maturity = "development"
roles = ["transaction_model", "integration_adapter", "physical_blackbox"]
''',
        '''[component.release]
export = "macro"
required_maturity = "development"
roles = ["interface_contract", "oa_port_contract", "circuit_netlist"]
''',
    )
    contract.write_text(source, encoding="utf-8")

    variant = contract.parent / "variants/default.toml"
    variant_source = variant.read_text(encoding="utf-8")
    variant_source = variant_source.replace(
        'fixture-ip = ["transaction_model"]',
        'fixture-ip = ["circuit_netlist"]',
    ).replace(
        '''[physical_binding]
kind = "oa-mixed-signal"
dependency = "fixture-ip"
transaction_module = "fixture_model"
physical_shell_module = "fixture_shell"
adapter_module = "fixture_adapter"
raw_macro_module = "fixture_macro"
status = "blocked"
blockers = ["implementation_release_missing"]
''',
        '''[physical_binding]
kind = "oa-native"
dependency = "fixture-ip"
transaction_module = "demo_transaction_model"
adapter_module = "demo_native_oa_adapter"
status = "blocked"
blockers = ["implementation_release_missing"]
''',
    )
    variant.write_text(variant_source, encoding="utf-8")

    manifest_path = artifact_root / manifest
    release_root = manifest_path.parent
    interface_path = release_root / "interfaces/interface.toml"
    interface_path.write_text(
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "fixture-ip"

[physical]
library = "fixture"
cell = "fixture_macro"
port_count = 1
canonical_port_contract = "ip/fixture/configs/ports.toml"

[behavior]
result = "native circuit response"

[supplies]
domains = []
''',
        encoding="utf-8",
    )
    for stale in (
        release_root / "rtl/fixture_macro.sv",
        release_root / "rtl/fixture_model.sv",
        release_root / "rtl/fixture_shell.sv",
    ):
        stale.unlink()
    (release_root / "rtl").rmdir()

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["owner"] = "fixture-ip"
    exported = payload["exports"][0]
    exported["oa"] = {
        "library": "fixture",
        "cell": "fixture_macro",
        "schematic_view": "schematic",
        "layout_view": "layout",
    }
    exported["interface"] = {
        "kind": "oa-native",
        "contract": "ip/fixture/configs/interface.toml",
    }
    selected_roles = {
        "interface_contract": "ip/fixture/configs/interface.toml",
        "oa_port_contract": "ip/fixture/configs/ports.toml",
        "circuit_netlist": "ip/fixture/circuit.scs",
    }
    exported["maturity"]["required_roles"] = list(selected_roles)
    payload["views"] = [
        view
        for view in payload["views"]
        if view["role"] in selected_roles
    ]
    for view in payload["views"]:
        view["source"] = selected_roles[view["role"]]
        selected = release_root / view["path"]
        view["size"] = selected.stat().st_size
        view["sha256"] = hashlib.sha256(selected.read_bytes()).hexdigest()
        if view["role"] == "circuit_netlist":
            view["format"] = "spectre-source"
            circuit = (release_root / view["path"]).read_bytes()
            view["composition"] = "reachable-spectre-hierarchy"
            view["subcircuits"] = ["fixture_macro"]
            view["primitive_masters"] = []
            view["sha256"] = hashlib.sha256(circuit).hexdigest()
    manifest_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    _refresh_lock_manifest_digest(contract, manifest_path)
    return manifest_path


def _refresh_lock_manifest_digest(contract: Path, manifest_path: Path) -> None:
    lock_path = contract.parent / "dependency.lock.toml"
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    object_id = f"sha256-{digest}"
    target = manifest_path.parent.parent / object_id
    if target != manifest_path.parent:
        manifest_path.parent.rename(target)
    _replace_lock_value(
        lock_path,
        "manifest_sha256",
        digest,
    )


def _replace_lock_value(lock_path: Path, field: str, value: str) -> None:
    source = lock_path.read_text(encoding="utf-8")
    marker = f'{field} = "'
    start = source.index(marker) + len(marker)
    end = source.index('"', start)
    lock_path.write_text(source[:start] + value + source[end:], encoding="utf-8")


def _write_ip_fixture(project_root: Path, release_id: str, manifest: str) -> Path:
    owner_root = project_root / "ip/demo"
    dependency_root = project_root / "ip/fixture"
    (owner_root / "configs/variants").mkdir(parents=True)
    (owner_root / "rtl").mkdir()
    (dependency_root / "configs").mkdir(parents=True)
    (dependency_root / "configs/ip.toml").write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture-ip"

name = "fixture-ip"
kind = "rtl-ip"
release_contract = "release"

[sources]
manifest = "ip/fixture/configs/ip.toml"
release = "ip/fixture/configs/release.toml"

[filesets]
source = ["manifest"]
''',
        encoding="utf-8",
    )
    (dependency_root / "configs/release.toml").write_text(
        '''schema = 2
contract_kind = "ip-release"
path_scope = "owner"
owner = "fixture-ip"
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

[filesets.simulation]
filelist = "ip/demo/rtl/simulation.f"
required_capability = "simulation"

[filesets.simulation.dependency_roles]
fixture-ip = ["transaction_model"]

[physical_binding]
kind = "oa-mixed-signal"
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
    manifest_path = project_root.parent / "artifacts" / manifest
    manifest_sha256 = (
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest_path.is_file()
        else "b" * 64
    )
    (owner_root / "configs/dependency.lock.toml").write_text(
        f"""schema = 3
contract_kind = "ip-dependency-lock"
path_scope = "owner"
owner = "demo"

ip = "demo"

[[dependency]]
name = "fixture-ip"
release_id = "{release_id}"
store = "fixture"
maturity = "development"
source_commit = "{'a' * 40}"
manifest_sha256 = "{manifest_sha256}"
""",
        encoding="utf-8",
    )
    contract = owner_root / "configs/ip.toml"
    contract.write_text(
        """schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "demo"

name = "demo"
kind = "composite-ip"
dependency_lock = "dependency_lock"

[[component]]
name = "fixture-ip"
contract = "ip/fixture/configs/ip.toml"

[component.release]
export = "macro"
required_maturity = "development"
roles = ["transaction_model", "integration_adapter", "physical_blackbox"]

[variants]
default = "default_variant"

[sources]
top = "ip/demo/rtl/top.sv"
simulation_filelist = "ip/demo/rtl/simulation.f"
default_variant = "ip/demo/configs/variants/default.toml"
dependency_lock = "ip/demo/configs/dependency.lock.toml"

[filesets]
rtl = ["top", "simulation_filelist", "default_variant"]
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
        f'''schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "repository"

[runtime.directories]
"release-store.fixture" = "{project_root.parent / 'artifacts/release-store'}"

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
    write_project_context(project_root)
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
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "leaf"

name = "leaf"
kind = "rtl-ip"

[sources]
rtl = "ip/leaf/rtl/leaf.sv"

[filesets]
rtl = ["rtl"]
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
    (owner / "configs/tool.toml").write_text(
        '''schema = 1
contract_kind = "ip-toolchain-contract"
path_scope = "owner"
owner = "composite"
''',
        encoding="utf-8",
    )
    contract = owner / "configs/ip.toml"
    contract.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "composite"

name = "composite"
kind = "composite-ip"

[implementation]
fixture = "tool"

[variants]
default = "default_variant"

[sources]
top = "ip/composite/rtl/top.sv"
simulation_filelist = "ip/composite/rtl/simulation.f"
default_variant = "ip/composite/configs/variants/default.toml"
tool = "ip/composite/configs/tool.toml"

[filesets]
rtl = ["top", "simulation_filelist", "default_variant"]

[[component]]
name = "leaf"
contract = "ip/leaf/configs/ip.toml"
''',
        encoding="utf-8",
    )
    (project_root / "catalogs/ip.toml").write_text(
        '''schema = 1
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "test"

[components.leaf]
contract = "ip/leaf/configs/ip.toml"
root = "ip/leaf"

[components.composite]
contract = "ip/composite/configs/ip.toml"
root = "ip/composite"
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
        project=Project.open(project_root).with_artifact_root(artifact_root),
        variant_name="default",
    )

    assert result["schema"] == 1
    assert result["contract_kind"] == "ip-integration-check"
    assert result["owner"] == "demo"
    assert result["passed"] is True
    assert "architecture" not in result
    assert _absolute_strings(result) == []
    assert "sources" not in result
    assert result["source_files"] == ["ip/demo/rtl/top.sv"]
    assert result["release_sources"] == [
        f"{Path(manifest).parent.as_posix()}/rtl/fixture_model.sv"
    ]
    selected = result["dependency_releases"][0]
    assert selected["store"] == "fixture"
    assert selected["manifest_sha256"] == Path(manifest).parts[3].removeprefix(
        "sha256-"
    )

    resolved = resolve_ip_integration_fileset(
        contract,
        project=Project.open(project_root).with_artifact_root(artifact_root),
        variant_name="default",
    )

    assert resolved == (
        (project_root / "ip/demo/rtl/top.sv").resolve(),
        (artifact_root / result["release_sources"][0]).resolve(),
    )
    assert all(path.is_absolute() and path.is_file() for path in resolved)


def test_integration_check_returns_a_portable_result(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    result = check_ip_integration(
        contract,
        project=Project.open(project_root).with_artifact_root(artifact_root),
        variant_name="default",
    )

    assert result["contract_kind"] == "ip-integration-check"
    assert result["ip"] == "demo"
    assert _absolute_strings(result) == []


def test_source_level_child_ip_is_selected_by_fileset_without_a_release_lock(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    contract = _write_source_component_fixture(project_root)

    plan = plan_ip_integration(
        contract,
        project=Project.open(project_root).with_artifact_root(
            tmp_path / "artifacts"
        ),
    )

    result = check_ip_integration(
        contract,
        project=Project.open(project_root).with_artifact_root(
            tmp_path / "artifacts"
        ),
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


def test_rtl_release_dependency_is_consumed_without_physical_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_rtl_release_fixture(artifact_root)
    contract_path = _write_ip_fixture(project_root, release_id, manifest)
    _select_rtl_dependency(contract_path)

    project = Project.open(project_root).with_artifact_root(
        artifact_root
    )
    contract = load_ip_integration_contract(
        contract_path,
        project=project,
    )
    release = contract.release_dependencies[0].release
    assert release is not None
    assert release.export == "rtl"

    producer_path = project_root / "ip/fixture/configs/release.toml"
    producer = SimpleNamespace(
        name="fixture-ip",
        path=producer_path,
        project=project,
    )
    monkeypatch.setattr(
        ip_integration,
        "plan_ip_release_contract",
        lambda *_args, **_kwargs: SimpleNamespace(record={
            "contract": "ip/fixture/configs/release.toml",
            "release_id": release_id,
            "exports": [
                {
                    "name": "rtl",
                    "interface": {"kind": "rtl", "module": "fixture_rtl"},
                }
            ],
            "collateral": [
                {
                    "export": "rtl",
                    "role": "rtl_source",
                    "module": "fixture_rtl",
                }
            ],
        }),
    )
    plan = plan_ip_integration(
        contract_path,
        project=project,
        release_inventory={"fixture-ip": producer},
    )
    assert "interface" not in plan["dependencies"][0]["release"]

    result = check_ip_integration(
        contract_path,
        project=project,
        variant_name="default",
    )

    assert result["passed"] is True
    assert result["dependency_releases"][0]["roles"] == {
        "rtl_source": f"{Path(manifest).parent.as_posix()}/rtl/fixture_rtl.sv"
    }
    assert result["release_sources"] == [
        f"{Path(manifest).parent.as_posix()}/rtl/fixture_rtl.sv"
    ]

def test_native_oa_release_dependency_is_typed_planned_and_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract_path = _write_ip_fixture(project_root, release_id, manifest)
    _select_native_oa_dependency(
        contract_path,
        artifact_root=artifact_root,
        manifest=manifest,
    )
    project = Project.open(project_root).with_artifact_root(
        artifact_root
    )

    contract = load_ip_integration_contract(contract_path, project=project)
    release = contract.release_dependencies[0].release
    assert release is not None
    assert release.export == "macro"
    binding = contract.get_variant("default").physical_binding
    assert isinstance(binding, OaNativePhysicalBinding)
    assert binding.transaction_module == "demo_transaction_model"

    producer_path = project_root / "ip/fixture/configs/release.toml"
    producer = SimpleNamespace(
        name="fixture-ip",
        path=producer_path,
        project=project,
    )
    monkeypatch.setattr(
        ip_integration,
        "plan_ip_release_contract",
        lambda *_args, **_kwargs: SimpleNamespace(record={
            "contract": "ip/fixture/configs/release.toml",
            "release_id": release_id,
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
                        "kind": "oa-native",
                        "contract": "ip/fixture/configs/interface.toml",
                    },
                }
            ],
            "collateral": [
                {"export": "macro", "role": role}
                for role in release.roles
            ],
        }),
    )

    plan = plan_ip_integration(
        contract_path,
        project=project,
        release_inventory={"fixture-ip": producer},
    )
    assert "interface" not in plan["dependencies"][0]["release"]
    assert plan["dependencies"][0]["release"]["roles"] == [
        "interface_contract",
        "oa_port_contract",
        "circuit_netlist",
    ]
    assert plan["variants"][0]["physical_binding"] == {
        "kind": "oa-native",
        "dependency": "fixture-ip",
        "transaction_module": "demo_transaction_model",
        "adapter_module": "demo_native_oa_adapter",
        "status": "blocked",
        "blockers": ["implementation_release_missing"],
    }

    result = check_ip_integration(
        contract_path,
        project=project,
        variant_name="default",
    )
    assert result["passed"] is True
    circuit = result["dependency_releases"][0]["roles"]["circuit_netlist"]
    assert circuit.startswith("release-store/fixture/objects/sha256-")
    assert circuit.endswith("/circuit/fixture_macro.scs")


def test_native_oa_planner_takes_interface_identity_from_provider_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract_path = _write_ip_fixture(project_root, release_id, manifest)
    _select_native_oa_dependency(
        contract_path,
        artifact_root=artifact_root,
        manifest=manifest,
    )
    project = Project.open(project_root).with_artifact_root(
        artifact_root
    )
    producer_path = project_root / "ip/fixture/configs/release.toml"
    producer = SimpleNamespace(
        name="fixture-ip",
        path=producer_path,
        project=project,
    )
    oa = {
        "library": "fixture",
        "cell": "fixture_macro",
        "schematic_view": "schematic",
        "layout_view": "layout",
    }
    interface = {
        "kind": "oa-native",
        "contract": "ip/fixture/configs/interface.toml",
    }
    oa["cell"] = "drifted"
    monkeypatch.setattr(
        ip_integration,
        "plan_ip_release_contract",
        lambda *_args, **_kwargs: SimpleNamespace(record={
            "contract": "ip/fixture/configs/release.toml",
            "release_id": release_id,
            "exports": [
                {
                    "name": "macro",
                    "oa": oa,
                    "interface": interface,
                }
            ],
            "collateral": [
                {"export": "macro", "role": role}
                for role in (
                    "interface_contract",
                    "oa_port_contract",
                    "circuit_netlist",
                )
            ],
        }),
    )

    plan = plan_ip_integration(
        contract_path,
        project=project,
        release_inventory={"fixture-ip": producer},
    )
    assert "interface" not in plan["dependencies"][0]["release"]


def test_ip_catalog_rejects_release_registries(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_source_component_fixture(project_root)
    rogue = project_root / "misc/release.toml"
    rogue.parent.mkdir()
    rogue.write_text("schema = 1\n", encoding="utf-8")
    catalog = project_root / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + '\n[targets.rogue]\ncontract = "misc/release.toml"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown fields.*targets"):
        Project.open(project_root)


def test_declaring_release_capability_does_not_implicitly_consume_it(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'dependency_lock = "dependency_lock"\n',
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
        project=Project.open(project_root).with_artifact_root(artifact_root),
        variant_name="default",
    )

    assert result["dependency_lock"] is None
    assert result["dependency_releases"] == []


def test_component_dependency_owns_its_typed_release_intent(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    component = _write_ip_fixture(project_root, release_id, manifest)

    contract = load_ip_integration_contract(
        component,
        project=Project.open(project_root).with_artifact_root(artifact_root),
    )

    release = contract.component.components[0].release
    assert release is not None
    assert release.export == "macro"
    assert release.required_maturity == "development"


def test_ip_filelist_contract_and_entries_have_distinct_safe_boundaries(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    variant = contract.parent / "variants/default.toml"
    foreign_filelist = project_root / "ip/fixture/configs/simulation.f"
    foreign_filelist.write_text("ip/demo/rtl/top.sv\n", encoding="utf-8")
    variant_source = variant.read_text(encoding="utf-8")
    variant.write_text(
        variant_source.replace(
            'filelist = "ip/demo/rtl/simulation.f"',
            'filelist = "ip/fixture/configs/simulation.f"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="filelist.*inside owner 'demo' root"):
        ip_integration.plan_ip_integration_fileset(
            contract,
            project=Project.open(project_root),
            variant_name="default",
        )

    variant.write_text(variant_source, encoding="utf-8")
    (project_root / "ip/demo/rtl/simulation.f").write_text(
        "ip/demo/rtl//top.sv\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="safe project-relative path"):
        ip_integration.plan_ip_integration_fileset(
            contract,
            project=Project.open(project_root),
            variant_name="default",
        )


def test_ip_filelist_content_is_identified_and_sources_are_unique(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    artifact_root = tmp_path / "artifacts"
    release_id, manifest = _write_release_fixture(artifact_root)
    contract = _write_ip_fixture(project_root, release_id, manifest)
    filelist = project_root / "ip/demo/rtl/simulation.f"

    first = ip_integration.plan_ip_integration_fileset(
        contract,
        project=Project.open(project_root),
        variant_name="default",
    )
    filelist.write_text("# selected closure\nip/demo/rtl/top.sv\n", encoding="utf-8")
    second = ip_integration.plan_ip_integration_fileset(
        contract,
        project=Project.open(project_root),
        variant_name="default",
    )

    assert first["sources"] == second["sources"]
    assert first["filelist_sha256"] != second["filelist_sha256"]
    assert second["filelist_size"] == filelist.stat().st_size

    filelist.write_text(
        "ip/demo/rtl/top.sv\nip/demo/rtl/top.sv\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="duplicate source"):
        ip_integration.plan_ip_integration_fileset(
            contract,
            project=Project.open(project_root),
            variant_name="default",
        )


def test_ip_integration_keeps_physical_readiness_separate_from_synthesis(
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

    with pytest.raises(RuntimeError, match="unavailable for synthesis"):
        check_ip_integration(
            contract,
            project=Project.open(project_root).with_artifact_root(artifact_root),
            variant_name="default",
        )

    variant.write_text(
        variant.read_text(encoding="utf-8").replace(
            'required_capability = "synthesis"',
            'required_capability = "physical_implementation"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="physical binding is blocked"):
        check_ip_integration(
            contract,
            project=Project.open(project_root).with_artifact_root(artifact_root),
            variant_name="default",
        )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("lock-identity", "lock identity"),
        ("interface-drift", "interface identities disagree"),
        ("module-drift", "module disagrees"),
        ("missing-manifest", "release store"),
        ("stale-role", "content drifted"),
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
    elif case == "interface-drift":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["exports"][0]["interface"]["logical"] = "wrong:interface"
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

    if case in {"interface-drift", "module-drift"}:
        _refresh_lock_manifest_digest(contract, manifest_path)

    with pytest.raises((RuntimeError, FileNotFoundError), match=message):
        check_ip_integration(
            contract,
            project=Project.open(project_root).with_artifact_root(artifact_root),
            variant_name="default",
        )
