from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from types import MappingProxyType, SimpleNamespace

import pytest

import sigilicon.domain.component as component_domain
import sigilicon.domain.config_contracts as config_contracts
import sigilicon.domain.design as design_domain
import sigilicon.domain.oa_library as oa_library_domain
import sigilicon.domain.oa_simulation as oa_simulation_domain
import sigilicon.domain.platform as platform_domain
import sigilicon.workflows.ip_packaging as ip_packaging
from sigilicon.domain.config_contracts import (
    RepositorySourceInventory,
    freeze_toml_document,
    inspect_project_configuration_sources,
)
from sigilicon.domain.ip_release import (
    OaMixedSignalIpInterface,
    OaNativeIpInterface,
    RtlIpInterface,
    load_ip_contract,
    resolve_ip_contract,
)
from sigilicon.domain.repository import Project
from sigilicon.workflows.ip_packaging import release_role_view

from conftest import write_project_context


def _contract_fixture(root: Path) -> Path:
    write_project_context(root)
    owner = root / "ip/fixture"
    configs = owner / "configs"
    sources = owner / "sources"
    configs.mkdir(parents=True)
    sources.mkdir()
    (configs / "oa.toml").write_text("name = 'fixture-lib'\n", encoding="utf-8")
    for name in ("left", "right"):
        (configs / f"{name}_interface.toml").write_text(
            f"name = '{name}'\n", encoding="utf-8"
        )
        (sources / f"{name}.toml").write_text(
            f"name = '{name}'\n", encoding="utf-8"
        )
    (configs / "ip.toml").write_text(
        """schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture"

name = "fixture-ip"
kind = "composite-ip"

[filesets]
left = ["ip/fixture/sources/left.toml"]
right = ["ip/fixture/sources/right.toml"]
""",
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        """schema = 1
contract_kind = "ip-release"
path_scope = "owner"
owner = "fixture"

name = "fixture-ip"
producer = "ip/fixture"
component = "configs/ip.toml"
default_maturity = "development"

[[exports]]
name = "left"
[exports.oa]
library = "fixture-lib"
cell = "LEFT"
schematic_view = "schematic"
layout_view = "layout"
[exports.interface]
kind = "oa-mixed-signal"
contract = "configs/left_interface.toml"
physical = "LEFT:physical"
logical = "left_model:logical"
[exports.maturity.development]
required_roles = ["interface_contract"]
[exports.maturity.implementation]
required_roles = ["interface_contract"]
[exports.maturity.signoff]
required_roles = ["interface_contract"]

[[exports]]
name = "right"
[exports.oa]
library = "fixture-lib"
cell = "RIGHT"
schematic_view = "schematic"
layout_view = "layout"
[exports.interface]
kind = "oa-mixed-signal"
contract = "configs/right_interface.toml"
physical = "RIGHT:physical"
logical = "right_model:logical"
[exports.maturity.development]
required_roles = ["interface_contract"]
[exports.maturity.implementation]
required_roles = ["interface_contract"]
[exports.maturity.signoff]
required_roles = ["interface_contract"]

[[collateral]]
export = "left"
role = "interface_contract"
component = "fixture-ip"
fileset = "left"
package_path = "exports/left/interface.toml"
format = "toml"

[[collateral]]
export = "right"
role = "interface_contract"
component = "fixture-ip"
fileset = "right"
package_path = "exports/right/interface.toml"
format = "toml"

[source]
oa_assembly = "ip/fixture/configs/oa.toml"
files = []
""",
        encoding="utf-8",
    )
    catalog = root / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + '''
[components.fixture-ip]
contract = "ip/fixture/configs/ip.toml"
root = "ip/fixture"

[targets.fixture-ip]
contract = "ip/fixture/configs/release.toml"
''',
        encoding="utf-8",
    )
    return contract


def _rtl_contract_fixture(root: Path) -> Path:
    write_project_context(root)
    owner = root / "ip/rtl_fixture"
    configs = owner / "configs"
    rtl = owner / "rtl"
    configs.mkdir(parents=True)
    rtl.mkdir()
    (rtl / "top.sv").write_text(
        "module rtl_top(input logic clk, output logic ready);\n"
        "  assign ready = clk;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (configs / "interface.toml").write_text(
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "rtl-fixture"

[module]
name = "rtl_top"
source = "ip/rtl_fixture/rtl/top.sv"

ports = [
  { name = "clk", direction = "input", width = 1 },
  { name = "ready", direction = "output", width = 1 },
]
''',
        encoding="utf-8",
    )
    (configs / "ip.toml").write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "rtl-fixture"

name = "rtl-fixture"
kind = "rtl-ip"
public_interface = "ip/rtl_fixture/configs/interface.toml"

[filesets]
interface = ["ip/rtl_fixture/configs/interface.toml"]
rtl = ["ip/rtl_fixture/rtl/top.sv"]
''',
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-release"
path_scope = "owner"
owner = "rtl-fixture"

name = "rtl-fixture"
producer = "ip/rtl_fixture"
component = "configs/ip.toml"
default_maturity = "development"

[[exports]]
name = "rtl-top"
[exports.interface]
kind = "rtl"
contract = "configs/interface.toml"
module = "rtl_top"
source_role = "rtl_source"
[exports.maturity.development]
required_roles = ["interface_contract", "rtl_source"]
[exports.maturity.implementation]
required_roles = ["interface_contract", "rtl_source", "synthesis_receipt"]
[exports.maturity.signoff]
required_roles = [
  "interface_contract",
  "rtl_source",
  "synthesis_receipt",
  "physical_implementation_receipt",
]

[[collateral]]
export = "rtl-top"
role = "interface_contract"
component = "rtl-fixture"
fileset = "interface"
package_path = "exports/rtl-top/interface.toml"
format = "toml"

[[collateral]]
export = "rtl-top"
role = "rtl_source"
component = "rtl-fixture"
fileset = "rtl"
package_path = "exports/rtl-top/rtl_top.sv"
format = "systemverilog"
module = "rtl_top"
capabilities = ["simulation", "synthesis", "physical_implementation"]

[source]
files = []
''',
        encoding="utf-8",
    )
    catalog = root / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + '''
[components.rtl-fixture]
contract = "ip/rtl_fixture/configs/ip.toml"
root = "ip/rtl_fixture"

[targets.rtl-fixture]
contract = "ip/rtl_fixture/configs/release.toml"
''',
        encoding="utf-8",
    )
    return contract


def _native_oa_contract_fixture(root: Path) -> Path:
    write_project_context(root)
    owner = root / "ip/native_fixture"
    configs = owner / "configs"
    sources = owner / "sources"
    configs.mkdir(parents=True)
    sources.mkdir()
    (configs / "oa.toml").write_text("name = 'native-lib'\n", encoding="utf-8")
    (configs / "interface.toml").write_text(
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "native-fixture"

[physical]
library = "native-lib"
cell = "NATIVE_TOP"
port_count = 2
canonical_port_contract = "ip/native_fixture/sources/design.toml"

[behavior]
result = "native circuit response"

[supplies]
domains = []
''',
        encoding="utf-8",
    )
    (sources / "design.toml").write_text(
        '''[ports]
order = ["IN", "OUT"]

[ports.directions]
IN = "input"
OUT = "output"
''',
        encoding="utf-8",
    )
    (sources / "circuit.scs").write_text(
        "subckt NATIVE_TOP IN OUT\n"
        "X0 (IN OUT) NATIVE_CHILD\n"
        "ends NATIVE_TOP\n",
        encoding="utf-8",
    )
    (sources / "child.scs").write_text(
        "subckt NATIVE_CHILD IN OUT\n"
        "M0 (OUT IN 0 0) nch_mac l=30n w=120n\n"
        "ends NATIVE_CHILD\n",
        encoding="utf-8",
    )
    (configs / "ip.toml").write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "native-fixture"

name = "native-fixture"
kind = "hard-macro"

[filesets]
interface = ["ip/native_fixture/configs/interface.toml"]
ports = ["ip/native_fixture/sources/design.toml"]
circuit = ["ip/native_fixture/sources/circuit.scs"]
circuit_dependencies = ["ip/native_fixture/sources/child.scs"]
''',
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-release"
path_scope = "owner"
owner = "native-fixture"

name = "native-fixture"
producer = "ip/native_fixture"
component = "configs/ip.toml"
default_maturity = "development"

[[exports]]
name = "native-top"
[exports.oa]
library = "native-lib"
cell = "NATIVE_TOP"
schematic_view = "schematic"
layout_view = "layout"
[exports.interface]
kind = "oa-native"
contract = "configs/interface.toml"
[exports.maturity.development]
required_roles = ["interface_contract", "oa_port_contract", "circuit_netlist"]
[exports.maturity.implementation]
required_roles = ["interface_contract", "oa_port_contract", "circuit_netlist"]
[exports.maturity.signoff]
required_roles = ["interface_contract", "oa_port_contract", "circuit_netlist"]

[[collateral]]
export = "native-top"
role = "interface_contract"
component = "native-fixture"
fileset = "interface"
package_path = "exports/native-top/interface.toml"
format = "toml"

[[collateral]]
export = "native-top"
role = "oa_port_contract"
component = "native-fixture"
fileset = "ports"
package_path = "exports/native-top/design.toml"
format = "toml"

[[collateral]]
export = "native-top"
role = "circuit_netlist"
component = "native-fixture"
fileset = "circuit"
package_path = "exports/native-top/circuit.scs"
format = "spectre-source"
capabilities = ["circuit_simulation"]

[source]
oa_assembly = "ip/native_fixture/configs/oa.toml"
files = []
''',
        encoding="utf-8",
    )
    catalog = root / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + '''
[components.native-fixture]
contract = "ip/native_fixture/configs/ip.toml"
root = "ip/native_fixture"

[targets.native-fixture]
contract = "ip/native_fixture/configs/release.toml"
''',
        encoding="utf-8",
    )
    return contract


def _native_oa_library_fixture(root: Path) -> SimpleNamespace:
    sources = root / "ip/native_fixture/sources"
    return SimpleNamespace(
        name="native-lib",
        primitive_masters=("nch_mac",),
        cells=tuple(
            SimpleNamespace(
                canonical_source=sources / filename,
                views=(SimpleNamespace(kind="spectre_netlist"),),
            )
            for filename in ("circuit.scs", "child.scs")
        ),
    )


def test_one_ip_contract_exposes_multiple_scoped_circuits(tmp_path: Path) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))

    assert contract.name == "fixture-ip"
    assert contract.owner == "fixture"
    assert [item.name for item in contract.exports] == ["left", "right"]
    left = contract.get_export("left").interface
    right = contract.get_export("right").interface
    assert isinstance(left, OaMixedSignalIpInterface)
    assert isinstance(right, OaMixedSignalIpInterface)
    assert left.cell == "LEFT"
    assert right.cell == "RIGHT"
    assert [item.role for item in contract.collateral] == [
        "interface_contract",
        "interface_contract",
    ]


def test_native_oa_release_keeps_its_domain_interface_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    exported = contract.get_export("native-top")
    assert isinstance(exported.interface, OaNativeIpInterface)

    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging, "_source_control", lambda _root: ("d" * 40, False)
    )
    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        lambda *_args, **_kwargs: _native_oa_library_fixture(tmp_path),
    )

    plan = ip_packaging.plan_ip_release_contract(contract)

    assert plan["missing_items"] == []
    assert plan["exports"] == [
        {
            "name": "native-top",
            "oa": {
                "library": "native-lib",
                "cell": "NATIVE_TOP",
                "schematic_view": "schematic",
                "layout_view": "layout",
            },
            "interface": {
                "kind": "oa-native",
                "contract": "ip/native_fixture/configs/interface.toml",
            },
            "maturity": {
                "required_roles": [
                    "interface_contract",
                    "oa_port_contract",
                    "circuit_netlist",
                ],
                "missing_items": [],
            },
            "availability": {
                "simulation": True,
                "synthesis": False,
                "physical_implementation": False,
            },
        }
    ]
    assert plan["maturity_checks"][1] == {
        "name": "development_interface_consistency:native-top",
        "export": "native-top",
        "passed": True,
        "interface_kind": "oa-native",
        "oa_library": "native-lib",
        "oa_cell": "NATIVE_TOP",
        "physical_port_count": 2,
        "native_oa_port_contract_checked": True,
    }
    circuit = next(
        item for item in plan["collateral"] if item["role"] == "circuit_netlist"
    )
    assert circuit["composition"] == "reachable-spectre-hierarchy"
    assert circuit["subcircuits"] == ["NATIVE_CHILD", "NATIVE_TOP"]
    assert circuit["primitive_masters"] == ["nch_mac"]

    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest = contract.project.artifact_root / built["manifest"]
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["exports"] == plan["exports"]
    circuit_path = ip_packaging.resolve_release_role(
        audited,
        manifest,
        "circuit_netlist",
        export="native-top",
    )
    packaged = circuit_path.read_text(encoding="utf-8")
    assert packaged.index("subckt NATIVE_CHILD") < packaged.index(
        "subckt NATIVE_TOP"
    )
    assert "nch_mac" in packaged


def test_native_oa_release_exposes_only_structural_synthesis_with_liberty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    owner = tmp_path / "ip/native_fixture"
    liberty = owner / "sources/NATIVE_TOP_structural.lib"
    liberty.write_text(
        "library (native_structural) { cell (NATIVE_TOP) { "
        "pin (IN) { direction : input; } "
        "pin (OUT) { direction : output; } } }\n",
        encoding="utf-8",
    )
    component = owner / "configs/ip.toml"
    component.write_text(
        component.read_text(encoding="utf-8")
        + 'structural_liberty = ["ip/native_fixture/sources/NATIVE_TOP_structural.lib"]\n',
        encoding="utf-8",
    )
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8")
        + '''
[[collateral]]
export = "native-top"
role = "raw_macro_liberty_or_db"
component = "native-fixture"
fileset = "structural_liberty"
package_path = "exports/native-top/synthesis/NATIVE_TOP_structural.lib"
format = "liberty"
library = "native-lib"
cell = "NATIVE_TOP"
view = "structural_liberty"
corner = "structural-uncharacterized"
capabilities = ["synthesis"]
''',
        encoding="utf-8",
    )
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging, "_source_control", lambda _root: ("e" * 40, False)
    )
    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        lambda *_args, **_kwargs: _native_oa_library_fixture(tmp_path),
    )

    plan = ip_packaging.plan_ip_release_contract(contract)

    assert plan["exports"][0]["availability"] == {
        "simulation": True,
        "synthesis": True,
        "physical_implementation": False,
    }
    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest = contract.project.artifact_root / built["manifest"]
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["exports"][0]["availability"] == plan["exports"][0][
        "availability"
    ]
    assert ip_packaging.resolve_release_role(
        audited,
        manifest,
        "raw_macro_liberty_or_db",
        export="native-top",
    ).read_text(encoding="utf-8") == liberty.read_text(encoding="utf-8")


def test_native_oa_release_rejects_circuit_port_order_drift(
    tmp_path: Path,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    (tmp_path / "ip/native_fixture/sources/circuit.scs").write_text(
        "subckt NATIVE_TOP OUT IN\nends NATIVE_TOP\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="circuit pin order"):
        ip_packaging._development_interface_check(
            contract,
            contract.get_export("native-top"),
        )


def test_native_oa_release_rejects_digital_interface_sections(
    tmp_path: Path,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    interface_path = tmp_path / "ip/native_fixture/configs/interface.toml"
    interface_path.write_text(
        interface_path.read_text(encoding="utf-8")
        + '''
[transaction_boundary]
module = "forged"
''',
        encoding="utf-8",
    )
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))

    with pytest.raises(
        ValueError, match="cannot declare digital transaction sections"
    ):
        ip_packaging._development_interface_check(
            contract,
            contract.get_export("native-top"),
        )


def test_native_oa_package_rejects_digital_interface_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging, "_source_control", lambda _root: ("d" * 40, False)
    )
    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        lambda *_args, **_kwargs: _native_oa_library_fixture(tmp_path),
    )
    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest_path = contract.project.artifact_root / built["manifest"]
    tampered_root = tmp_path / "tampered-native-release"
    shutil.copytree(manifest_path.parent, tampered_root)
    tampered_manifest = tampered_root / "manifest.json"
    tampered_manifest.chmod(0o600)
    manifest = json.loads(tampered_manifest.read_text(encoding="utf-8"))
    interface_view = release_role_view(
        manifest,
        "interface_contract",
        export="native-top",
    )
    interface_path = tampered_root / str(interface_view["path"])
    interface_path.chmod(0o600)
    interface_path.write_text(
        interface_path.read_text(encoding="utf-8")
        + '''
[physical_macro]
module = "forged"
''',
        encoding="utf-8",
    )
    interface_view["size"] = interface_path.stat().st_size
    tampered_manifest.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError, match="cannot declare digital transaction sections"
    ):
        ip_packaging.audit_ip_release_manifest(tampered_manifest)


def test_native_oa_package_rejects_missing_reachable_subcircuit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging, "_source_control", lambda _root: ("d" * 40, False)
    )
    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        lambda *_args, **_kwargs: _native_oa_library_fixture(tmp_path),
    )
    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest_path = contract.project.artifact_root / built["manifest"]
    tampered_root = tmp_path / "tampered-native-hierarchy"
    shutil.copytree(manifest_path.parent, tampered_root)
    tampered_manifest = tampered_root / "manifest.json"
    tampered_manifest.chmod(0o600)
    manifest = json.loads(tampered_manifest.read_text(encoding="utf-8"))
    circuit_view = release_role_view(
        manifest,
        "circuit_netlist",
        export="native-top",
    )
    circuit_path = tampered_root / str(circuit_view["path"])
    circuit_path.chmod(0o600)
    source = circuit_path.read_text(encoding="utf-8")
    source = source[: source.index("subckt NATIVE_CHILD")] + source[
        source.index("subckt NATIVE_TOP") :
    ]
    circuit_path.write_text(source, encoding="utf-8")
    circuit_view["size"] = circuit_path.stat().st_size
    circuit_view["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    tampered_manifest.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing master NATIVE_CHILD"):
        ip_packaging.audit_ip_release_manifest(tampered_manifest)


def test_rtl_release_plans_and_audits_without_oa_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    exported = contract.get_export("rtl-top")
    assert isinstance(exported.interface, RtlIpInterface)
    assert contract.oa_assembly is None

    def reject_oa_load(*_args, **_kwargs):
        raise AssertionError("RTL release consulted an OA source")

    monkeypatch.setattr(
        oa_library_domain, "load_oa_library_source", reject_oa_load
    )
    monkeypatch.setattr(
        oa_library_domain, "resolve_oa_library_source", reject_oa_load
    )
    monkeypatch.setattr(
        ip_packaging, "_source_control", lambda _root: ("a" * 40, False)
    )

    plan = ip_packaging.plan_ip_release_contract(contract)

    assert plan["exports"] == [
        {
            "name": "rtl-top",
            "interface": {
                "kind": "rtl",
                "contract": "ip/rtl_fixture/configs/interface.toml",
                "module": "rtl_top",
                "source_role": "rtl_source",
            },
            "maturity": {
                "required_roles": ["interface_contract", "rtl_source"],
                "missing_items": [],
            },
            "availability": {
                "simulation": True,
                "synthesis": False,
                "physical_implementation": False,
            },
        }
    ]
    implementation = ip_packaging.plan_ip_release_contract(
        contract, maturity="implementation"
    )
    assert implementation["missing_items"] == [
        "rtl-top:synthesis_receipt"
    ]
    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest = contract.project.artifact_root / built["manifest"]
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["exports"] == plan["exports"]

    def copy_manifest(name: str) -> tuple[Path, dict]:
        tampered_root = tmp_path / "tampered" / name
        shutil.copytree(manifest.parent, tampered_root)
        tampered_manifest = tampered_root / "manifest.json"
        tampered_manifest.chmod(0o600)
        payload = json.loads(tampered_manifest.read_text(encoding="utf-8"))
        return tampered_manifest, payload

    oa_manifest, oa_payload = copy_manifest("oa-injection")
    oa_payload["exports"][0]["oa"] = {
        "library": "forged",
        "cell": "FORGED",
    }
    oa_manifest.write_text(
        json.dumps(oa_payload, indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="cannot declare OA"):
        ip_packaging.audit_ip_release_manifest(oa_manifest)

    drifted_manifest, drifted_payload = copy_manifest("contract-drift")
    drifted_payload["exports"][0]["interface"]["contract"] = (
        "ip/rtl_fixture/configs/other.toml"
    )
    drifted_manifest.write_text(
        json.dumps(drifted_payload, indent=2) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="contract provenance drifted"):
        ip_packaging.audit_ip_release_manifest(drifted_manifest)


def test_release_interface_kind_controls_oa_source_contract(
    tmp_path: Path,
) -> None:
    untagged_path = _contract_fixture(tmp_path / "untagged-oa")
    untagged_path.write_text(
        untagged_path.read_text(encoding="utf-8").replace(
            'kind = "oa-mixed-signal"\n', "", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="interface.kind"):
        load_ip_contract(
            untagged_path,
            project=Project.from_project_root(tmp_path / "untagged-oa"),
        )

    rtl_path = _rtl_contract_fixture(tmp_path / "rtl-with-oa")
    rtl_path.write_text(
        rtl_path.read_text(encoding="utf-8").replace(
            "[exports.interface]\n",
            '''[exports.oa]
library = "forged"
cell = "FORGED"
schematic_view = "schematic"
layout_view = "layout"
[exports.interface]
''',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot declare an OA identity"):
        load_ip_contract(rtl_path, project=Project.from_project_root(tmp_path / "rtl-with-oa"))

    rtl_assembly_path = _rtl_contract_fixture(tmp_path / "rtl-assembly")
    rtl_assembly_path.write_text(
        rtl_assembly_path.read_text(encoding="utf-8").replace(
            "[source]\nfiles = []",
            '[source]\noa_assembly = "ip/rtl_fixture/configs/oa.toml"\nfiles = []',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot declare source.oa_assembly"):
        load_ip_contract(rtl_assembly_path, project=Project.from_project_root(tmp_path / "rtl-assembly"))

    oa_path = _contract_fixture(tmp_path / "oa-without-assembly")
    oa_path.write_text(
        oa_path.read_text(encoding="utf-8").replace(
            'oa_assembly = "ip/fixture/configs/oa.toml"\n', ""
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source.oa_assembly"):
        load_ip_contract(oa_path, project=Project.from_project_root(tmp_path / "oa-without-assembly"))


def test_rtl_release_variant_selects_its_declared_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    interface_path = tmp_path / "ip/rtl_fixture/configs/interface.toml"
    interface_path.write_text(
        interface_path.read_text(encoding="utf-8")
        + '''
[variant_modules.alternate]
name = "rtl_alternate"
source = "ip/rtl_fixture/rtl/top.sv"
''',
        encoding="utf-8",
    )
    rtl_path = tmp_path / "ip/rtl_fixture/rtl/top.sv"
    rtl_path.write_text(
        rtl_path.read_text(encoding="utf-8").replace(
            "rtl_top", "rtl_alternate"
        ),
        encoding="utf-8",
    )
    source = contract_path.read_text(encoding="utf-8")
    source = source.replace(
        'module = "rtl_top"\nsource_role = "rtl_source"',
        'module = "rtl_alternate"\n'
        'source_role = "rtl_source"\n'
        'variant = "alternate"',
        1,
    ).replace('module = "rtl_top"', 'module = "rtl_alternate"', 1)
    contract_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        ip_packaging, "_source_control", lambda _root: ("b" * 40, False)
    )

    contract = load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
    exported = contract.get_export("rtl-top")
    assert isinstance(exported.interface, RtlIpInterface)
    assert exported.interface.variant == "alternate"

    plan = ip_packaging.plan_ip_release_contract(contract)
    assert plan["exports"][0]["interface"]["variant"] == "alternate"
    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest = contract.project.artifact_root / built["manifest"]
    assert ip_packaging.audit_ip_release_manifest(manifest)["exports"] == (
        plan["exports"]
    )


def test_ip_contract_reuses_explicit_project(tmp_path: Path) -> None:
    contract_path = _contract_fixture(tmp_path)
    project = Project.from_project_root(tmp_path)

    contract = load_ip_contract(contract_path, project=project)

    assert contract.project is project
    assert contract.project_root == tmp_path
    assert contract.component_graph == {"fixture-ip": project.owner("fixture").component}
    assert contract.component_graph["fixture-ip"] is project.owner("fixture").component
    assert contract.document["name"] == "fixture-ip"
    interface_path = (tmp_path / "ip/fixture/configs/left_interface.toml").resolve()
    assert contract.interface_documents[interface_path]["name"] == "left"
    with pytest.raises(TypeError):
        contract.document["exports"][0]["name"] = "other"
    with pytest.raises(TypeError):
        contract.interface_documents[interface_path]["name"] = "other"


def test_development_interface_check_reuses_contract_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))

    def reject_reload(*_args, **_kwargs):
        raise AssertionError("interface contract was reloaded")

    monkeypatch.setattr(ip_packaging.tomllib, "load", reject_reload)

    with pytest.raises(ValueError, match="physical_macro must be a table"):
        ip_packaging._development_interface_check(
            contract,
            contract.get_export("left"),
        )


def test_ip_contract_rejects_partial_or_mutable_interface_snapshots(
    tmp_path: Path,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))
    left_path = (tmp_path / "ip/fixture/configs/left_interface.toml").resolve()
    incomplete = replace(
        contract,
        interface_documents=MappingProxyType(
            {left_path: contract.interface_documents[left_path]}
        ),
    )

    with pytest.raises(ValueError, match="interface document identity drift"):
        resolve_ip_contract(
            contract.path,
            project=contract.project,
            snapshot=incomplete,
        )

    mutable = replace(
        contract,
        interface_documents=MappingProxyType(
            {
                path: dict(document)
                for path, document in contract.interface_documents.items()
            }
        ),
    )
    with pytest.raises(ValueError, match="interface document is mutable"):
        resolve_ip_contract(
            contract.path,
            project=contract.project,
            snapshot=mutable,
        )

    interface = contract.exports[0].interface
    assert isinstance(interface, OaMixedSignalIpInterface)
    forged_export = replace(
        contract.exports[0], interface=replace(interface, cell="FORGED")
    )
    forged = replace(
        contract,
        exports=(forged_export, *contract.exports[1:]),
    )
    with pytest.raises(ValueError, match="typed contract drift"):
        resolve_ip_contract(
            contract.path,
            project=contract.project,
            snapshot=forged,
        )

    mutable_roles = replace(
        contract.exports[0],
        required_roles=dict(contract.exports[0].required_roles),
    )
    mutable_release = replace(
        contract,
        exports=(mutable_roles, *contract.exports[1:]),
    )
    with pytest.raises(ValueError, match="typed contract is mutable"):
        resolve_ip_contract(
            contract.path,
            project=contract.project,
            snapshot=mutable_release,
        )

    with pytest.raises(ValueError, match="source document is missing"):
        resolve_ip_contract(
            contract.path,
            project=contract.project,
            snapshot=replace(contract, document="forged"),
        )


def test_ip_contract_rejects_an_incomplete_interface_snapshot(
    tmp_path: Path,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))
    incomplete = replace(contract, interface_documents=MappingProxyType({}))
    with pytest.raises(ValueError, match="interface document identity drift"):
        resolve_ip_contract(
            incomplete.path,
            project=incomplete.project,
            snapshot=incomplete,
        )


def test_project_configuration_reuses_ip_release_interface_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))
    snapshot_paths = set(contract.interface_documents) | {contract.path}
    snapshot_reads: list[Path] = []
    original_read_toml = config_contracts.read_toml

    def counted_read_toml(path: Path):
        if path.resolve() in snapshot_paths:
            snapshot_reads.append(path.resolve())
        return original_read_toml(path)

    monkeypatch.setattr(config_contracts, "read_toml", counted_read_toml)

    project = contract.project
    target_catalogs = tuple(
        project.owner_target_catalog(owner)
        for owner in project.owners
        if owner.component.target_catalog is not None
    )
    sources = RepositorySourceInventory.for_project(project)
    sources.verify(
        "IP release snapshot",
        {contract.path: contract.document, **contract.interface_documents},
    )
    report = inspect_project_configuration_sources(
        project,
        target_catalog_inventory=target_catalogs,
        sources=sources,
    )

    assert report["passed"] is True
    assert snapshot_reads == []


def test_oa_port_contract_reuses_release_owned_design_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))
    source = (tmp_path / "ip/fixture/configs/left_interface.toml").resolve()
    document = freeze_toml_document(
        {
            "ports": {
                "order": ["A"],
                "directions": {"A": "input"},
            }
        }
    )
    snapshot = SimpleNamespace(
        project=contract.project,
        path=source,
        source_documents=MappingProxyType({source: document}),
    )

    def resolve(path, *, project, snapshot):
        assert path == source
        assert project is contract.project
        return snapshot

    monkeypatch.setattr(design_domain, "resolve_design_spec", resolve)
    monkeypatch.setattr(
        ip_packaging.tomllib,
        "load",
        lambda *_args, **_kwargs: pytest.fail("OA port contract was reloaded"),
    )

    reused = ip_packaging._oa_port_contract_document(
        contract,
        source,
        design_inventory={source: snapshot},
    )

    assert reused is document
    assert ip_packaging._oa_port_contract(reused)["A"].direction == "input"
    with pytest.raises(ValueError, match="inventory has no"):
        ip_packaging._oa_port_contract_document(
            contract,
            source,
            design_inventory={},
        )
    outside = (tmp_path / "outside.toml").resolve()
    outside.write_text("name = 'outside'\n", encoding="utf-8")
    outside_snapshot = SimpleNamespace(
        source_documents=MappingProxyType({outside: document}),
    )
    monkeypatch.setattr(
        design_domain,
        "resolve_design_spec",
        lambda *_args, **_kwargs: outside_snapshot,
    )
    with pytest.raises(ValueError, match="inside the release producer"):
        ip_packaging._oa_port_contract_document(
            contract,
            outside,
            design_inventory={outside: outside_snapshot},
        )


def test_release_design_inventory_rejects_forged_oa_plan(
    tmp_path: Path,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))
    oa_manifest = (tmp_path / "ip/fixture/configs/oa.toml").resolve()
    declared = (tmp_path / "ip/fixture/configs/left_interface.toml").resolve()
    forged = (tmp_path / "ip/fixture/configs/right_interface.toml").resolve()
    source = SimpleNamespace(
        project=contract.project,
        name="fixture-lib",
        pdk="testpdk",
        cells=(SimpleNamespace(cell="LEFT", design_spec=declared),),
    )
    forged_spec = SimpleNamespace(
        path=forged,
        project=contract.project,
        library="fixture-lib",
        cell="LEFT",
        pdk=SimpleNamespace(key="testpdk"),
    )
    plan = SimpleNamespace(
        source=source,
        designs=(SimpleNamespace(inspection=SimpleNamespace(spec=forged_spec)),),
    )

    with pytest.raises(ValueError, match="undeclared design spec"):
        ip_packaging._release_design_inventory(
            contract,
            {oa_manifest: plan},
        )


def test_release_consumers_reuse_the_contract_component_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.from_project_root(tmp_path))

    def reject_graph_reload(*_args, **_kwargs):
        raise AssertionError("release consumer reloaded the component graph")

    monkeypatch.setattr(component_domain, "load_component_graph", reject_graph_reload)
    cells = []
    for name in ("LEFT", "RIGHT"):
        source = tmp_path / f"{name}.scs"
        source.write_text(f"subckt {name} A\nends {name}\n", encoding="utf-8")
        cells.append(
            SimpleNamespace(
                cell=name,
                owner="fixture",
                role="design",
                canonical_source=source,
                source_manifest_path=contract.path,
                manifest_path=contract.path,
                design_spec=None,
                layout_specs=(),
                views=(
                    SimpleNamespace(
                        kind="spectre_netlist",
                        source=source,
                        dependencies=(),
                    ),
                ),
            )
        )
    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        lambda *_args, **_kwargs: SimpleNamespace(
            name="fixture-lib",
            cells=tuple(cells),
            manifest_path=tmp_path / "ip/fixture/configs/oa.toml",
            project=contract.project,
        ),
    )
    monkeypatch.setattr(
        ip_packaging,
        "_source_control",
        lambda _root: ("a" * 40, False),
    )
    monkeypatch.setattr(
        ip_packaging,
        "_development_interface_check",
        lambda _contract, exported: {
            "name": f"development_interface_consistency:{exported.name}",
            "export": exported.name,
            "passed": True,
        },
    )
    monkeypatch.setattr(
        ip_packaging,
        "_qualification_semantics",
        lambda *_args, **_kwargs: ({"name": "qualification", "passed": True}, []),
    )
    def reject_contract_reload(*_args, **_kwargs):
        raise AssertionError("typed release planner reloaded its contract")

    monkeypatch.setattr(ip_packaging, "load_ip_contract", reject_contract_reload)

    sources = ip_packaging._source_inputs(contract)
    plan = ip_packaging.plan_ip_release_contract(contract)

    assert "ip/fixture/configs/ip.toml" in sources
    assert plan["component"]["name"] == "fixture-ip"


def test_release_source_inventory_reuses_one_platform_for_all_testbenches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.from_file(write_project_context(tmp_path))
    release = tmp_path / "release.toml"
    assembly = tmp_path / "oa.toml"
    cell_manifest = tmp_path / "cell.toml"
    netlist = tmp_path / "top.scs"
    setup = tmp_path / "simulation.toml"
    layout = tmp_path / "layout.toml"
    generator = tmp_path / "layout_generator.py"
    generator_dependency = tmp_path / "generator_dependency.py"
    generator_package = tmp_path / "generator_support"
    generator_package.mkdir()
    generator_package_init = generator_package / "__init__.py"
    generator_support = generator_package / "recipe.py"
    dependency_netlist = tmp_path / "dependency.scs"
    for path in (release, assembly, cell_manifest, setup):
        path.write_text("name = 'fixture'\n", encoding="utf-8")
    netlist.write_text("subckt TOP A\nends TOP\n", encoding="utf-8")
    generator.write_text("VALUE = 1\n", encoding="utf-8")
    generator_dependency.write_text("VALUE = 2\n", encoding="utf-8")
    generator_package_init.write_text("VALUE = 3\n", encoding="utf-8")
    generator_support.write_text("VALUE = 4\n", encoding="utf-8")
    dependency_netlist.write_text("subckt DEP A\nends DEP\n", encoding="utf-8")
    layout.write_text(
        """[layout]
generator_dependencies = ["generator_dependency.py"]
generator_modules = ["generator_support.recipe", "sigilicon.workflows.ip_packaging"]
source_netlist = "top.scs"
dependency_netlists = ["dependency.scs"]
""",
        encoding="utf-8",
    )
    top = SimpleNamespace(
        cell="TOP",
        owner="fixture",
        role="design",
        canonical_source=netlist,
        source_manifest_path=assembly,
        manifest_path=cell_manifest,
        design_spec=None,
        layout_specs=(layout,),
        views=(
            SimpleNamespace(
                kind="spectre_netlist",
                source=netlist,
                dependencies=(),
            ),
        ),
    )
    testbenches = tuple(
        SimpleNamespace(
            cell=f"TB_{index}",
            owner="fixture",
            role="testbench",
            canonical_source=setup,
            source_manifest_path=assembly,
            manifest_path=cell_manifest,
            design_spec=None,
            layout_specs=(),
            views=tuple(
                SimpleNamespace(
                    kind=kind,
                    source=setup,
                    dependencies=(SimpleNamespace(cell="TOP"),),
                )
                for kind in ("config", "maestro")
            ),
        )
        for index in range(2)
    )
    library = SimpleNamespace(
        name="fixture",
        pdk="testpdk",
        project=project,
        manifest_path=assembly,
        cells=(top, *testbenches),
    )
    contract = SimpleNamespace(
        project=project,
        project_root=tmp_path,
        path=release,
        component_graph={},
        source_files=(),
        oa_assembly=Path("oa.toml"),
        exports=(
            SimpleNamespace(
                name="top",
                interface=OaMixedSignalIpInterface(
                    kind="oa-mixed-signal",
                    contract=PurePosixPath("interface.toml"),
                    library="fixture",
                    cell="TOP",
                    schematic_view="schematic",
                    layout_view="layout",
                    physical="TOP:physical",
                    logical="top_model:logical",
                ),
            ),
        ),
    )
    platform = object()
    platform_reads: list[tuple[object, str]] = []
    simulation_platforms: list[object] = []

    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        lambda *_args, **_kwargs: library,
    )

    def load_release_platform(repository, key):
        platform_reads.append((repository, key))
        return platform

    monkeypatch.setattr(platform_domain, "load_platform", load_release_platform)

    def load_simulation(_path, *, project, platform):
        assert project is library.project
        simulation_platforms.append(platform)
        return SimpleNamespace(
            native_setup=SimpleNamespace(rdb_contract=None),
        )

    monkeypatch.setattr(
        oa_simulation_domain,
        "load_oa_simulation_spec",
        load_simulation,
    )

    ip_packaging._source_inputs(contract)

    assert platform_reads == [(project, "testpdk")]
    assert simulation_platforms == [platform, platform]

    platform_reads.clear()
    simulation_platforms.clear()
    resolved_platforms: list[tuple[object, str, object]] = []

    def resolve_release_platform(repository, key, *, snapshot):
        resolved_platforms.append((repository, key, snapshot))
        return snapshot

    monkeypatch.setattr(
        platform_domain,
        "resolve_platform",
        resolve_release_platform,
    )

    ip_packaging._source_inputs(
        contract,
        platform_inventory={"testpdk": platform},
    )

    assert platform_reads == []
    assert resolved_platforms == [(project, "testpdk", platform)]
    assert simulation_platforms == [platform, platform]

    simulation_platforms.clear()
    resolved_platforms.clear()
    resolved_oa_sources: list[tuple[Path, object, object]] = []

    def reject_oa_source_load(*_args, **_kwargs):
        raise AssertionError("explicit inventory must replace OA source I/O")

    def resolve_release_oa_source(path, *, project, snapshot):
        resolved_oa_sources.append((path, project, snapshot))
        return snapshot

    monkeypatch.setattr(
        oa_library_domain,
        "load_oa_library_source",
        reject_oa_source_load,
    )
    monkeypatch.setattr(
        oa_library_domain,
        "resolve_oa_library_source",
        resolve_release_oa_source,
    )

    inventory_sources = ip_packaging._source_inputs(
        contract,
        platform_inventory={"testpdk": platform},
        oa_source_inventory={assembly: library},
    )

    assert resolved_oa_sources == [(assembly, project, library)]
    assert simulation_platforms == [platform, platform]

    simulation_platforms.clear()
    planned_simulations = tuple(
        SimpleNamespace(
            path=setup,
            project=project,
            library="fixture",
            cell=cell.cell,
            native_setup=SimpleNamespace(rdb_contract=None),
        )
        for cell in testbenches
    )
    oa_plan = SimpleNamespace(
        source=library,
        library="fixture",
        testbenches=tuple(
            SimpleNamespace(
                cell=cell.cell,
                canonical_source=cell.canonical_source,
                simulation=simulation,
            )
            for cell, simulation in zip(
                testbenches,
                planned_simulations,
                strict=True,
            )
        ),
        layouts=(
            SimpleNamespace(
                spec=SimpleNamespace(
                    path=layout,
                    project=project,
                    library="fixture",
                    cell="TOP",
                    generator_source=generator,
                    generator_dependencies=(generator_dependency,),
                    generator_modules=(
                        "generator_support.recipe",
                        "sigilicon.workflows.ip_packaging",
                    ),
                    generator_module_sources=(
                        generator_support,
                        Path(ip_packaging.__file__).resolve(),
                    ),
                    source_netlist=netlist,
                    dependency_netlists=(dependency_netlist,),
                ),
            ),
        ),
    )

    def reject_simulation_load(*_args, **_kwargs):
        raise AssertionError("explicit OA plan must replace simulation I/O")

    def reject_layout_load(*_args, **_kwargs):
        raise AssertionError("explicit OA plan must replace layout TOML I/O")

    with monkeypatch.context() as plan_patch:
        plan_patch.setattr(
            oa_simulation_domain,
            "load_oa_simulation_spec",
            reject_simulation_load,
        )
        plan_patch.setattr(ip_packaging.tomllib, "load", reject_layout_load)
        planned_sources = ip_packaging._source_inputs(
            contract,
            oa_source_inventory={assembly: library},
            oa_plan_inventory={assembly: oa_plan},
        )

    assert simulation_platforms == []
    assert planned_sources == inventory_sources

    generator_support.unlink()
    with pytest.raises(FileNotFoundError, match="release source is missing"):
        ip_packaging._source_inputs(
            contract,
            oa_source_inventory={assembly: library},
            oa_plan_inventory={assembly: oa_plan},
        )
    generator_support.write_text("VALUE = 4\n", encoding="utf-8")

    with pytest.raises(ValueError, match="OA plan inventory has no"):
        ip_packaging._source_inputs(
            contract,
            oa_source_inventory={assembly: library},
            oa_plan_inventory={},
        )

    with pytest.raises(ValueError, match="OA source inventory has no"):
        ip_packaging._source_inputs(
            contract,
            platform_inventory={"testpdk": platform},
            oa_source_inventory={},
        )

    with pytest.raises(
        ValueError,
        match="platform inventory has no 'testpdk' entry",
    ):
        ip_packaging._source_inputs(
            contract,
            platform_inventory={},
            oa_source_inventory={assembly: library},
        )


def test_ip_contract_owner_must_match_cataloged_owner(tmp_path: Path) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'owner = "fixture"', 'owner = "other"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner"):
        load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))


def test_release_roles_are_unique_within_an_export_not_across_ip(
    tmp_path: Path,
) -> None:
    manifest = {
        "exports": [{"name": "left"}, {"name": "right"}],
        "views": [
            {"export": "left", "role": "transaction_model", "module": "left"},
            {"export": "right", "role": "transaction_model", "module": "right"},
        ],
    }

    assert release_role_view(
        manifest, "transaction_model", export="left"
    )["module"] == "left"
    assert release_role_view(
        manifest, "transaction_model", export="right"
    )["module"] == "right"
    with pytest.raises(KeyError, match="missing"):
        release_role_view(manifest, "transaction_model", export="missing")


def test_single_endpoint_release_schema_is_not_a_compatibility_path(
    tmp_path: Path,
) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        """schema = 1
contract_kind = "ip-release"
path_scope = "owner"
owner = "fixture"

name = "fixture-ip"
producer = "ip/fixture"
component = "configs/ip.toml"
default_maturity = "development"
[oa]
library = "fixture-lib"
cell = "LEFT"
[source]
oa_assembly = "ip/fixture/configs/oa.toml"
files = []
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exports"):
        load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))


def test_release_maturity_is_not_a_qualification_compatibility_alias(
    tmp_path: Path,
) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8")
        .replace("default_maturity", "default_qualification")
        .replace("exports.maturity", "exports.qualification"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="maturity"):
        load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))


def test_release_identity_cannot_alias_one_component_as_another_ip(
    tmp_path: Path,
) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'name = "fixture-ip"', 'name = "split-endpoint"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="component identity"):
        load_ip_contract(contract_path, project=Project.from_project_root(tmp_path))
