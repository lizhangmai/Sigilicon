from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest

import sigilicon.domain.oa_library as oa_library_domain
import sigilicon.adapters.release.ip_packaging as ip_packaging
from sigilicon.artifacts import SafeTree
from sigilicon.domain.ip_release import (
    OaMixedSignalIpInterface,
    OaNativeIpInterface,
    RtlIpInterface,
    load_ip_contract,
)
from sigilicon.project import Project
from sigilicon.adapters.release.ip_packaging import release_role_view

from conftest import write_project_context, write_test_platform


def _built_manifest(project: Project, built: dict[str, object]) -> Path:
    return (
        project.artifact_root
        / "release-store"
        / str(built["store"])
        / "objects"
        / f"sha256-{built['manifest_sha256']}"
        / "manifest.json"
    )


def _publish_release(
    contract_path: Path,
    *,
    project: Project,
    maturity: str | None = None,
) -> dict[str, object]:
    owner = project.require_owner(contract_path).name
    selector = f"{owner}:release"
    plan = project.plan(selector)
    if maturity is not None:
        planned = plan.steps[0].config.get("maturity")
        if planned != maturity:
            raise ValueError(
                f"fixture operation maturity is {planned!r}, expected {maturity!r}"
            )
    result = project.run(plan, run_id=uuid.uuid4().hex)
    if result.status != "succeeded":
        raise RuntimeError(f"release run did not succeed: {result.status}")
    return json.loads(
        result.outcomes[0].result.artifacts[0].read_text()
    )


def _write_release_operation(configs: Path, owner: str) -> None:
    (configs.parents[2] / "artifacts/release-store").mkdir(
        parents=True,
        exist_ok=True,
    )
    (configs / "operations.toml").write_text(
        f'''schema = 4
contract_kind = "owner-operations"
path_scope = "owner"
owner = "{owner}"

[operations.release]
uses = "sigilicon.ip-release"
filesets = ["release"]
config = {{ owner = "{owner}", maturity = "development" }}
''',
        encoding="utf-8",
    )


def test_readonly_release_tree_rejects_symlink_members(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o700)
    (staging / "link").symlink_to(outside, target_is_directory=True)

    try:
        with pytest.raises(RuntimeError, match="symlinks"):
            SafeTree(staging).make_readonly()
        assert outside.stat().st_mode & 0o777 == 0o700
    finally:
        outside.chmod(0o700)
        staging.chmod(0o700)
        (staging / "link").unlink(missing_ok=True)


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
        'owner = "fixture-ip"\n'
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
        """schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture-ip"

name = "fixture-ip"
kind = "composite-ip"
release_contract = "release"
operation_catalog = "operations"

[sources]
oa = "ip/fixture/configs/oa.toml"
release = "ip/fixture/configs/release.toml"
operations = "ip/fixture/configs/operations.toml"
left = "ip/fixture/sources/left.toml"
right = "ip/fixture/sources/right.toml"

[filesets]
oa_source = ["oa"]
release = ["release"]
""",
        encoding="utf-8",
    )
    contract = configs / "release.toml"
    contract.write_text(
        """schema = 2
contract_kind = "ip-release"
path_scope = "owner"
owner = "fixture-ip"

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
    _write_release_operation(configs, "fixture-ip")
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
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "rtl-fixture"

name = "rtl-fixture"
kind = "rtl-ip"
public_interface = "interface"
release_contract = "release"
operation_catalog = "operations"

[sources]
interface = "ip/rtl_fixture/configs/interface.toml"
release = "ip/rtl_fixture/configs/release.toml"
operations = "ip/rtl_fixture/configs/operations.toml"
rtl = "ip/rtl_fixture/rtl/top.sv"

[filesets]
release = ["release"]
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
    _write_release_operation(configs, "rtl-fixture")
    return contract


def test_release_publication_runs_as_one_managed_adapter_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rtl_contract_fixture(tmp_path)
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="a" * 40,
            working_tree_dirty=False,
        ),
    )
    project = Project.open(tmp_path)

    plan = project.plan("rtl-fixture:release")
    checked = project.preflight(plan)
    result = project.run(plan, run_id="a" * 32)

    assert checked.ready
    assert result.status == "succeeded"
    step = result.outcomes[0].result
    assert step.facts["release_id"] == f"development-{'a' * 40}"
    assert step.facts["passed"] is True
    assert step.artifacts[0].kind == "summary.ip-release"


def _native_oa_contract_fixture(root: Path) -> Path:
    write_project_context(root)
    owner = root / "ip/native_fixture"
    configs = owner / "configs"
    sources = owner / "sources"
    configs.mkdir(parents=True)
    sources.mkdir()
    (root / "virtuoso").mkdir()
    write_test_platform(root)
    (configs / "oa.toml").write_text(
        "schema = 1\n"
        'contract_kind = "oa-assembly"\n'
        'path_scope = "owner"\n'
        'owner = "native-fixture"\n'
        'name = "native_lib"\n'
        'pdk = "testpdk"\n'
        'primitive_masters = ["nch_mac"]\n'
        'cell_roots = ["sources"]\n',
        encoding="utf-8",
    )
    (configs / "interface.toml").write_text(
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "native-fixture"

[physical]
library = "native_lib"
cell = "NATIVE_TOP"
port_count = 2
canonical_port_contract = "ip/native_fixture/sources/NATIVE_TOP/design.toml"

[behavior]
result = "native circuit response"

[supplies]
domains = []
''',
        encoding="utf-8",
    )
    top = sources / "NATIVE_TOP"
    child = sources / "NATIVE_CHILD"
    top.mkdir()
    child.mkdir()
    (top / "design.toml").write_text(
        '''[ports]
order = ["IN", "OUT"]

[ports.directions]
IN = "input"
OUT = "output"
''',
        encoding="utf-8",
    )
    (top / "circuit.scs").write_text(
        "subckt NATIVE_TOP IN OUT\n"
        "X0 (IN OUT) NATIVE_CHILD\n"
        "ends NATIVE_TOP\n",
        encoding="utf-8",
    )
    (child / "design.toml").write_text(
        "[ports]\norder = [\"IN\", \"OUT\"]\n",
        encoding="utf-8",
    )
    (child / "circuit.scs").write_text(
        "subckt NATIVE_CHILD IN OUT\n"
        "M0 (OUT IN 0 0) nch_mac l=30n w=120n\n"
        "ends NATIVE_CHILD\n",
        encoding="utf-8",
    )
    for cell, directory, netlist_dependencies in (
        ("NATIVE_TOP", top, '["NATIVE_CHILD/netlist"]'),
        ("NATIVE_CHILD", child, "[]"),
    ):
        (directory / "cell.toml").write_text(
            f'''schema = 1
contract_kind = "oa-cell"
path_scope = "cell"
owner = "native-fixture"

cell = "{cell}"
role = "design"
canonical_source = "circuit.scs"
views = [
  {{ name = "netlist", kind = "spectre_netlist", source = "circuit.scs", dependencies = {netlist_dependencies} }},
  {{ name = "schematic", kind = "schematic", source = "design.toml", dependencies = ["{cell}/netlist"] }},
  {{ name = "symbol", kind = "symbol", source = "design.toml", dependencies = ["{cell}/schematic"] }},
]
''',
            encoding="utf-8",
        )
    (configs / "ip.toml").write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "native-fixture"

name = "native-fixture"
kind = "hard-macro"
release_contract = "release"
operation_catalog = "operations"

[sources]
oa = "ip/native_fixture/configs/oa.toml"
release = "ip/native_fixture/configs/release.toml"
operations = "ip/native_fixture/configs/operations.toml"
interface = "ip/native_fixture/configs/interface.toml"
ports = "ip/native_fixture/sources/NATIVE_TOP/design.toml"
circuit = "ip/native_fixture/sources/NATIVE_TOP/circuit.scs"
circuit_dependency = "ip/native_fixture/sources/NATIVE_CHILD/circuit.scs"

[filesets]
oa_source = ["oa"]
release = ["release"]
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
library = "native_lib"
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
    _write_release_operation(configs, "native-fixture")
    return contract


def test_one_ip_contract_exposes_multiple_scoped_circuits(tmp_path: Path) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project=Project.open(tmp_path))

    assert contract.name == "fixture-ip"
    assert contract.owner == "fixture-ip"
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
            'release_contract = "release"\n',
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
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
    )
    plan = ip_packaging.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )

    assert plan.record["missing_items"] == []
    assert plan.record["exports"] == [
        {
            "name": "native-top",
            "oa": {
                "library": "native_lib",
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
    assert plan.record["maturity_checks"][1] == {
        "name": "development_interface_consistency:native-top",
        "export": "native-top",
        "passed": True,
        "interface_kind": "oa-native",
        "oa_library": "native_lib",
        "oa_cell": "NATIVE_TOP",
        "physical_port_count": 2,
        "native_oa_port_contract_checked": True,
    }
    circuit = next(
        item for item in plan.record["collateral"] if item["role"] == "circuit_netlist"
    )
    assert circuit["composition"] == "reachable-spectre-hierarchy"
    assert circuit["subcircuits"] == ["NATIVE_CHILD", "NATIVE_TOP"]
    assert circuit["primitive_masters"] == ["nch_mac"]

    built = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    assert built["store"] == "native-fixture"
    assert "object" not in built
    assert "manifest" not in built
    manifest = _built_manifest(Project.open(tmp_path), built)
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["schema"] == 2
    assert audited["release_id"] == f"development-{'d' * 40}"
    assert set(audited["provenance"]) == {"contract", "producer", "generator"}
    assert all(len(view["sha256"]) == 64 for view in audited["views"])
    snapshot = manifest.read_bytes()
    repeated = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    assert {
        key: repeated[key] for key in ("store", "manifest_sha256")
    } == {
        key: built[key] for key in ("store", "manifest_sha256")
    }
    assert manifest.read_bytes() == snapshot
    assert audited["exports"] == plan.record["exports"]
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


def test_release_build_rejects_checkout_drift_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    states = iter(
        (
            SimpleNamespace(commit="d" * 40, working_tree_dirty=False),
            SimpleNamespace(commit="d" * 40, working_tree_dirty=True),
        )
    )
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: next(states),
    )

    with pytest.raises(RuntimeError, match="changed during"):
        _publish_release(
            contract_path,
            project=Project.open(tmp_path),
        )

    object_root = tmp_path / "artifacts/release-store/rtl-fixture/objects"
    assert not object_root.exists() or not tuple(object_root.iterdir())


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
library = "native_lib"
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
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="e" * 40, working_tree_dirty=False
        ),
    )
    plan = ip_packaging.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )

    assert plan.record["exports"][0]["availability"] == {
        "simulation": True,
        "synthesis": True,
        "physical_implementation": False,
    }
    built = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    manifest = _built_manifest(Project.open(tmp_path), built)
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["exports"][0]["availability"] == plan.record["exports"][0][
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    project = Project.open(tmp_path)
    contract = load_ip_contract(contract_path, project=project)
    (tmp_path / "ip/native_fixture/sources/NATIVE_TOP/circuit.scs").write_text(
        "subckt NATIVE_TOP OUT IN\nends NATIVE_TOP\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
    )
    with pytest.raises(ValueError, match="circuit pin order"):
        ip_packaging.plan_ip_release_contract(
            contract,
            project=project,
        )


def test_native_oa_package_rejects_digital_interface_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    monkeypatch.setattr(
        ip_packaging,
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
    )
    built = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    manifest_path = _built_manifest(Project.open(tmp_path), built)
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
        "inspect_checkout",
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
    )
    built = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    manifest_path = _built_manifest(Project.open(tmp_path), built)
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

    plan = ip_packaging.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )

    assert plan.record["exports"] == [
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
        contract,
        project=Project.open(tmp_path),
        maturity="implementation",
    )
    assert implementation.record["missing_items"] == [
        "rtl-top:synthesis_receipt"
    ]
    built = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    manifest = _built_manifest(Project.open(tmp_path), built)
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["exports"] == plan.record["exports"]

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

    plan = ip_packaging.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )
    assert plan.record["exports"][0]["interface"]["variant"] == "alternate"
    built = _publish_release(
        contract_path,
        project=Project.open(tmp_path),
    )
    manifest = _built_manifest(Project.open(tmp_path), built)
    assert ip_packaging.audit_ip_release_manifest(manifest)["exports"] == (
        plan.record["exports"]
    )


def test_ip_contract_owner_must_match_cataloged_owner(tmp_path: Path) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'owner = "fixture-ip"', 'owner = "other"', 1
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
