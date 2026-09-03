from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import sigilicon.domain.oa_library as oa_library_domain
import sigilicon.workflows.ip_packaging as ip_packaging
from sigilicon.domain.ip_release import (
    OaMixedSignalIpInterface,
    OaNativeIpInterface,
    RtlIpInterface,
    load_ip_contract,
)
from sigilicon.project import Project
from sigilicon.workflows.ip_packaging import release_role_view

from conftest import write_project_context, write_test_layout_platform


def _built_manifest(project: Project, built: dict[str, object]) -> Path:
    return (
        project.artifact_root
        / "release-store"
        / str(built["store"])
        / "objects"
        / f"sha256-{built['manifest_sha256']}"
        / "manifest.json"
    )


def _contract_fixture(root: Path) -> Path:
    write_project_context(root)
    owner = root / "ip/fixture"
    configs = owner / "configs"
    sources = owner / "sources"
    configs.mkdir(parents=True)
    sources.mkdir()
    (configs / "oa.toml").write_text(
        "schema = 1\n"
        'contract_kind = "oa-assembly"\n'
        'path_scope = "owner"\n'
        'owner = "fixture"\n'
        'name = "fixture-lib"\n',
        encoding="utf-8",
    )
    for name in ("left", "right"):
        (configs / f"{name}_interface.toml").write_text(
            f"name = '{name}'\n", encoding="utf-8"
        )
        (sources / f"{name}.toml").write_text(
            f"name = '{name}'\n", encoding="utf-8"
        )
    (configs / "ip.toml").write_text(
        """schema = 2
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture"

name = "fixture-ip"
kind = "composite-ip"
release_contract = "ip/fixture/configs/release.toml"

[sources]
oa = "ip/fixture/configs/oa.toml"
left = "ip/fixture/sources/left.toml"
right = "ip/fixture/sources/right.toml"

[filesets]
oa_source = ["oa"]
""",
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        """schema = 2
contract_kind = "ip-release"
path_scope = "owner"
owner = "fixture"

name = "fixture-ip"
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
source = "left"
package_path = "exports/left/interface.toml"
format = "toml"

[[collateral]]
export = "right"
role = "interface_contract"
component = "fixture-ip"
source = "right"
package_path = "exports/right/interface.toml"
format = "toml"

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
        '''schema = 2
contract_kind = "ip-component"
path_scope = "owner"
owner = "rtl-fixture"

name = "rtl-fixture"
kind = "rtl-ip"
public_interface = "ip/rtl_fixture/configs/interface.toml"
release_contract = "ip/rtl_fixture/configs/release.toml"

[sources]
interface = "ip/rtl_fixture/configs/interface.toml"
rtl = "ip/rtl_fixture/rtl/top.sv"
''',
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        '''schema = 2
contract_kind = "ip-release"
path_scope = "owner"
owner = "rtl-fixture"

name = "rtl-fixture"
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
source = "interface"
package_path = "exports/rtl-top/interface.toml"
format = "toml"

[[collateral]]
export = "rtl-top"
role = "rtl_source"
component = "rtl-fixture"
source = "rtl"
package_path = "exports/rtl-top/rtl_top.sv"
format = "systemverilog"
module = "rtl_top"
capabilities = ["simulation", "synthesis", "physical_implementation"]

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
''',
        encoding="utf-8",
    )
    return contract


def _oa_source_closure_fixture(root: Path) -> Path:
    contract = _contract_fixture(root)
    owner = root / "ip/fixture"
    configs = owner / "configs"
    sources = owner / "sources"
    (root / "virtuoso").mkdir()
    write_test_layout_platform(root)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'library = "fixture-lib"', 'library = "fixture_lib"'
        ),
        encoding="utf-8",
    )
    (configs / "oa.toml").write_text(
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "fixture"

name = "fixture_lib"
pdk = "testpdk"
primitive_masters = []
cell_roots = ["sources"]
''',
        encoding="utf-8",
    )
    for cell in ("LEFT", "RIGHT"):
        cell_root = sources / cell
        cell_root.mkdir()
        (cell_root / "circuit.scs").write_text(
            f"subckt {cell} IN OUT\nends {cell}\n",
            encoding="utf-8",
        )
        (cell_root / "design.toml").write_text(
            f"name = '{cell}'\n",
            encoding="utf-8",
        )
        (cell_root / "cell.toml").write_text(
            f'''schema = 1
contract_kind = "oa-cell"
path_scope = "cell"
owner = "fixture"

cell = "{cell}"
role = "design"
canonical_source = "circuit.scs"
views = [
  {{ name = "netlist", kind = "spectre_netlist", source = "circuit.scs", dependencies = [] }},
  {{ name = "schematic", kind = "schematic", source = "design.toml", dependencies = ["{cell}/netlist"] }},
  {{ name = "symbol", kind = "symbol", source = "design.toml", dependencies = ["{cell}/schematic"] }},
]
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
    (configs / "oa.toml").write_text(
        "schema = 1\n"
        'contract_kind = "oa-assembly"\n'
        'path_scope = "owner"\n'
        'owner = "native-fixture"\n'
        'name = "native-lib"\n',
        encoding="utf-8",
    )
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
        '''schema = 2
contract_kind = "ip-component"
path_scope = "owner"
owner = "native-fixture"

name = "native-fixture"
kind = "hard-macro"
release_contract = "ip/native_fixture/configs/release.toml"

[sources]
oa = "ip/native_fixture/configs/oa.toml"
interface = "ip/native_fixture/configs/interface.toml"
ports = "ip/native_fixture/sources/design.toml"
circuit = "ip/native_fixture/sources/circuit.scs"
circuit_dependency = "ip/native_fixture/sources/child.scs"

[filesets]
oa_source = ["oa"]
''',
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        '''schema = 2
contract_kind = "ip-release"
path_scope = "owner"
owner = "native-fixture"

name = "native-fixture"
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
source = "interface"
package_path = "exports/native-top/interface.toml"
format = "toml"

[[collateral]]
export = "native-top"
role = "oa_port_contract"
component = "native-fixture"
source = "ports"
package_path = "exports/native-top/design.toml"
format = "toml"

[[collateral]]
export = "native-top"
role = "circuit_netlist"
component = "native-fixture"
source = "circuit"
package_path = "exports/native-top/circuit.scs"
format = "spectre-source"
capabilities = ["circuit_simulation"]

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
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.open(tmp_path))

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


def test_oa_release_derives_complete_platform_source_closure(tmp_path: Path) -> None:
    contract_path = _oa_source_closure_fixture(tmp_path)
    platform = tmp_path / "configs/platform/testpdk"
    manifest = platform / "platform.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "\n[contracts]\n",
            '\nasset_scope = "external"\n\n[contracts]\n',
        ),
        encoding="utf-8",
    )
    project_manifest = tmp_path / "sigilicon.toml"
    project_manifest.write_text(
        project_manifest.read_text(encoding="utf-8")
        + f'\n[runtime.directories]\n"platform.testpdk" = "{platform}"\n',
        encoding="utf-8",
    )
    contract = load_ip_contract(
        contract_path,
        project=Project.open(tmp_path),
    )

    sources = set(ip_packaging._source_inputs(contract))

    assert {
        "configs/platform/catalog.toml",
        "configs/platform/testpdk/platform.toml",
        "configs/platform/testpdk/simulation.toml",
        "configs/platform/testpdk/oa.toml",
        "configs/platform/testpdk/layout.toml",
        "configs/platform/testpdk/verification.toml",
    }.issubset(sources)
    assert {
        "ip/fixture/configs/left_interface.toml",
        "ip/fixture/configs/right_interface.toml",
        "ip/fixture/configs/oa.toml",
        "ip/fixture/sources/LEFT/cell.toml",
        "ip/fixture/sources/RIGHT/cell.toml",
    }.issubset(sources)


def test_release_rejects_removed_owner_and_source_fields(tmp_path: Path) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'name = "rtl-fixture"\n',
            'name = "rtl-fixture"\nproducer = "ip/rtl_fixture"\n',
            1,
        )
        + "\n[source]\nfiles = []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown fields.*producer.*source"):
        load_ip_contract(contract_path, project=Project.open(tmp_path))


@pytest.mark.parametrize(
    ("fixture", "needle", "replacement", "label"),
    [
        (
            "rtl",
            'name = "rtl-top"\n',
            'name = "rtl-top"\nunexpected = "value"\n',
            r"exports\[0\]",
        ),
        (
            "rtl",
            'kind = "rtl"\n',
            'kind = "rtl"\nunexpected = "value"\n',
            r"exports\[0\]\.interface",
        ),
        (
            "oa",
            'library = "fixture-lib"\n',
            'library = "fixture-lib"\nunexpected = "value"\n',
            r"exports\[0\]\.oa",
        ),
        (
            "rtl",
            "[exports.maturity.development]\n",
            '[exports.maturity]\nunexpected = "value"\n'
            "[exports.maturity.development]\n",
            r"exports\[0\]\.maturity",
        ),
        (
            "rtl",
            "[exports.maturity.development]\n",
            '[exports.maturity.development]\nunexpected = "value"\n',
            r"exports\[0\]\.maturity\.development",
        ),
        (
            "rtl",
            "[[collateral]]\n",
            '[[collateral]]\nunexpected = "value"\n',
            r"collateral\[0\]",
        ),
    ],
)
def test_release_rejects_unknown_nested_fields(
    tmp_path: Path,
    fixture: str,
    needle: str,
    replacement: str,
    label: str,
) -> None:
    contract_path = (
        _contract_fixture(tmp_path)
        if fixture == "oa"
        else _rtl_contract_fixture(tmp_path)
    )
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            needle,
            replacement,
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=label + ".*unknown fields"):
        load_ip_contract(contract_path, project=Project.open(tmp_path))


def test_release_must_be_declared_by_its_owner_component(tmp_path: Path) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    component = tmp_path / "ip/rtl_fixture/configs/ip.toml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            'release_contract = "ip/rtl_fixture/configs/release.toml"\n',
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not declared by its owner"):
        load_ip_contract(contract_path, project=Project.open(tmp_path))


def test_native_oa_release_keeps_its_domain_interface_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
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
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
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
    assert built["store"] == "native-fixture"
    assert "object" not in built
    assert "manifest" not in built
    manifest = _built_manifest(contract.project, built)
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["schema"] == 2
    assert audited["release_id"] == f"development-{'d' * 40}"
    assert set(audited["provenance"]) == {"contract", "producer", "generator"}
    assert all(len(view["sha256"]) == 64 for view in audited["views"])
    snapshot = manifest.read_bytes()
    repeated = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    assert {
        key: repeated[key] for key in ("store", "manifest_sha256")
    } == {
        key: built[key] for key in ("store", "manifest_sha256")
    }
    assert manifest.read_bytes() == snapshot
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
        component.read_text(encoding="utf-8").replace(
            "\n[filesets]\n",
            "\nstructural_liberty = "
            '"ip/native_fixture/sources/NATIVE_TOP_structural.lib"\n\n'
            "[filesets]\n",
        ),
        encoding="utf-8",
    )
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8")
        + '''
[[collateral]]
export = "native-top"
role = "raw_macro_liberty_or_db"
component = "native-fixture"
source = "structural_liberty"
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
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="e" * 40, working_tree_dirty=False
        ),
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
    manifest = _built_manifest(contract.project, built)
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
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    (tmp_path / "ip/native_fixture/sources/circuit.scs").write_text(
        "subckt NATIVE_TOP OUT IN\nends NATIVE_TOP\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="circuit pin order"):
        ip_packaging._development_interface_check(
            contract,
            contract.get_export("native-top"),
        )


def test_native_oa_package_rejects_digital_interface_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
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
    manifest_path = _built_manifest(contract.project, built)
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
    interface_view["sha256"] = hashlib.sha256(interface_path.read_bytes()).hexdigest()
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
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "_source_inputs",
        lambda *_args, **_kwargs: (
            "ip/native_fixture/configs/release.toml",
        ),
    )
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
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
    manifest_path = _built_manifest(contract.project, built)
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
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    exported = contract.get_export("rtl-top")
    assert isinstance(exported.interface, RtlIpInterface)
    assert contract.oa_assembly is None

    def reject_oa_load(*_args, **_kwargs):
        raise AssertionError("RTL release consulted an OA source")

    monkeypatch.setattr(
        oa_library_domain, "load_oa_library_source", reject_oa_load
    )
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="a" * 40, working_tree_dirty=False
        ),
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
    manifest = _built_manifest(contract.project, built)
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
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="b" * 40, working_tree_dirty=False
        ),
    )

    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    exported = contract.get_export("rtl-top")
    assert isinstance(exported.interface, RtlIpInterface)
    assert exported.interface.variant == "alternate"

    plan = ip_packaging.plan_ip_release_contract(contract)
    assert plan["exports"][0]["interface"]["variant"] == "alternate"
    built = ip_packaging.build_ip_release(
        contract_path,
        project=contract.project,
    )
    manifest = _built_manifest(contract.project, built)
    assert ip_packaging.audit_ip_release_manifest(manifest)["exports"] == (
        plan["exports"]
    )


def test_release_design_inventory_rejects_forged_oa_plan(
    tmp_path: Path,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.open(tmp_path))
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


def test_ip_contract_owner_must_match_cataloged_owner(tmp_path: Path) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'owner = "fixture"', 'owner = "other"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner"):
        load_ip_contract(contract_path, project=Project.open(tmp_path))


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
        load_ip_contract(contract_path, project=Project.open(tmp_path))
