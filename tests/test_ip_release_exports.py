from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import re
import tomllib
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import pytest

import sigilicon.domain.oa_library as oa_library_domain
import sigilicon.adapters.release.ip_packaging as ip_packaging
import sigilicon.adapters.release.ip_release_planning as ip_release_planning
from sigilicon.adapters.release import source_control
from sigilicon.artifacts import SafeTree
from sigilicon.domain.ip_release import (
    MixedSignalIpInterface,
    CircuitIpInterface,
    RtlIpInterface,
    load_ip_contract,
    resolve_ip_contract,
)
from sigilicon.project import Project
from sigilicon.adapters.release.ip_packaging import release_view
from sigilicon.adapters.release.release_plan_record import IpReleaseRecord

from conftest import write_project_context, write_test_platform


def _patch_checkout(
    monkeypatch: pytest.MonkeyPatch,
    callback: Callable[..., object],
) -> None:
    state = None

    def query(root: Path, resources, *arguments: str) -> str:
        nonlocal state
        if arguments == ("rev-parse", "HEAD"):
            state = callback(root, resources)
            return state.commit + "\n"
        if arguments[0] == "status":
            return " M fixture\n" if state.working_tree_dirty else ""
        if arguments == ("rev-parse", "--show-prefix"):
            return ""
        if arguments[0] == "ls-tree":
            rows = []
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()
                    digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
                    rows.append(f"100644 blob {digest}\t{path.relative_to(root).as_posix()}\0")
            return "".join(rows)
        raise AssertionError(f"unexpected Git query: {arguments}")

    monkeypatch.setattr(source_control, "_git_output", query)


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
    project_contract = configs.parents[2] / "sigilicon.toml"
    with project_contract.open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"vcs.git" = "{shutil.which("git")}"\n')
    (configs.parents[2] / "artifacts/release-store").mkdir(
        parents=True,
        exist_ok=True,
    )
    (configs / "operations.toml").write_text(
        f'''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "{owner}"

[operations.release]
uses = "sigilicon.ip-release"
filesets = [{{ component = "{owner}", fileset = "release" }}]
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
        """schema = 6
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture-ip"
root = "ip/fixture"

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
        """schema = 5
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
kind = "mixed-signal"
bindings = { interface_contract = "interface_contract" }
contract = "configs/left_interface.toml"
physical = "LEFT:physical"
logical = "left_model:logical"
[exports.maturity.development]
required_views = ["interface_contract"]
[exports.maturity.implementation]
required_views = ["interface_contract"]
[exports.maturity.signoff]
required_views = ["interface_contract"]

[[exports]]
name = "right"
[exports.oa]
library = "fixture-lib"
cell = "RIGHT"
schematic_view = "schematic"
layout_view = "layout"
[exports.interface]
kind = "mixed-signal"
bindings = { interface_contract = "interface_contract" }
contract = "configs/right_interface.toml"
physical = "RIGHT:physical"
logical = "right_model:logical"
[exports.maturity.development]
required_views = ["interface_contract"]
[exports.maturity.implementation]
required_views = ["interface_contract"]
[exports.maturity.signoff]
required_views = ["interface_contract"]

[[collateral]]
export = "left"
name = "interface_contract"
role = "interface_contract"
component = "fixture-ip"
source = "left"
package_path = "exports/left/interface.toml"
format = "toml"

[[collateral]]
export = "right"
name = "interface_contract"
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
        '''schema = 6
contract_kind = "ip-component"
path_scope = "owner"
owner = "rtl-fixture"
root = "ip/rtl_fixture"

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
        '''schema = 5
contract_kind = "ip-release"
path_scope = "owner"
owner = "rtl-fixture"

name = "rtl-fixture"
default_maturity = "development"

[[exports]]
name = "rtl-top"
[exports.interface]
kind = "rtl"
bindings = { interface_contract = "interface_contract" }
contract = "configs/interface.toml"
module = "rtl_top"
source_view = "rtl_source"
[exports.receipts.synthesis_receipt]
authority = "offline-fixture"
inputs = ["interface_contract", "rtl_source"]
outputs = []
[exports.receipts.physical_implementation_receipt]
authority = "offline-fixture"
inputs = ["interface_contract", "rtl_source"]
outputs = []
[exports.maturity.development]
required_views = ["interface_contract", "rtl_source"]
[exports.maturity.implementation]
required_views = ["interface_contract", "rtl_source", "synthesis_receipt"]
[exports.maturity.signoff]
required_views = [
  "interface_contract",
  "rtl_source",
  "synthesis_receipt",
  "physical_implementation_receipt",
]

[[collateral]]
export = "rtl-top"
name = "interface_contract"
role = "interface_contract"
component = "rtl-fixture"
source = "interface"
package_path = "exports/rtl-top/interface.toml"
format = "toml"

[[collateral]]
export = "rtl-top"
name = "rtl_source"
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
    _patch_checkout(
        monkeypatch,
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
    assert step.artifacts[0].kind == "summary.ip-release"
    summary = json.loads(step.artifacts[0].read_text())
    assert summary["release_id"] == f"development-{'a' * 40}"
    assert summary["store"] == "rtl-fixture"


def test_ip_release_snapshot_rejects_current_contract_drift(tmp_path: Path) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    project = Project.open(tmp_path)
    snapshot = load_ip_contract(contract_path, project=project)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'default_maturity = "development"',
            'default_maturity = "implementation"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="IP release snapshot source document drift"):
        resolve_ip_contract(contract_path, project=project, snapshot=snapshot)


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
        '''schema = 6
contract_kind = "ip-component"
path_scope = "owner"
owner = "native-fixture"
root = "ip/native_fixture"

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
        '''schema = 5
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
kind = "circuit"
bindings = { interface_contract = "interface_contract", oa_port_contract = "oa_port_contract", circuit_netlist = "circuit_netlist" }
contract = "configs/interface.toml"
[exports.maturity.development]
required_views = ["interface_contract", "oa_port_contract", "circuit_netlist"]
[exports.maturity.implementation]
required_views = ["interface_contract", "oa_port_contract", "circuit_netlist"]
[exports.maturity.signoff]
required_views = ["interface_contract", "oa_port_contract", "circuit_netlist"]

[[collateral]]
export = "native-top"
name = "interface_contract"
role = "interface_contract"
component = "native-fixture"
source = "interface"
package_path = "exports/native-top/interface.toml"
format = "toml"

[[collateral]]
export = "native-top"
name = "oa_port_contract"
role = "oa_port_contract"
component = "native-fixture"
source = "ports"
package_path = "exports/native-top/design.toml"
format = "toml"

[[collateral]]
export = "native-top"
name = "circuit_netlist"
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
    assert isinstance(left, MixedSignalIpInterface)
    assert isinstance(right, MixedSignalIpInterface)
    assert left.authoring.cell == "LEFT"
    assert right.authoring.cell == "RIGHT"
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
    assert isinstance(exported.interface, CircuitIpInterface)

    _patch_checkout(
        monkeypatch,
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
    )
    plan = ip_release_planning.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )

    assert isinstance(plan.payload, IpReleaseRecord)
    detached_record = plan.record
    detached_record["exports"] = []
    assert plan.record["exports"]
    assert plan.record["missing_items"] == []
    release_export = plan.payload.exports[0]
    assert release_export.name == "native-top"
    assert release_export.interface.kind == "circuit"
    assert (release_export.oa.library, release_export.oa.cell) == ("native_lib", "NATIVE_TOP")
    assert release_export.availability.simulation
    assert not release_export.availability.physical_implementation
    assert plan.record["maturity_checks"][1] == {
        "name": "development_interface_consistency:native-top",
        "export": "native-top",
        "passed": True,
        "interface_kind": "circuit",
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
    circuit_path = ip_packaging.resolve_release_view(
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
    _patch_checkout(
        monkeypatch,
        lambda _root, _resources: next(states),
    )

    with pytest.raises(RuntimeError, match="changed during"):
        _publish_release(
            contract_path,
            project=Project.open(tmp_path),
        )

    object_root = tmp_path / "artifacts/release-store/rtl-fixture/objects"
    assert not object_root.exists() or not tuple(object_root.iterdir())


@pytest.mark.parametrize(("capability", "view_format", "synthesis"), [
    ("synthesis", "liberty", True),
    ("diagnostic", "liberty", False),
    ("synthesis", "text", False),
])
def test_native_oa_release_exposes_only_structural_synthesis_with_liberty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    view_format: str,
    synthesis: bool,
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
name = "raw_macro_liberty_or_db"
role = "raw_macro_liberty_or_db"
component = "native-fixture"
source = "structural_liberty"
package_path = "exports/native-top/synthesis/NATIVE_TOP_structural.lib"
format = "liberty"
library = "native_lib"
cell = "NATIVE_TOP"
view = "structural_liberty"
condition = { corner = "structural-uncharacterized" }
capabilities = ["synthesis"]
''',
        encoding="utf-8",
    )
    contract_path.write_text(contract_path.read_text().replace(
        'capabilities = ["synthesis"]', f'capabilities = ["{capability}"]'
    ).replace('format = "liberty"', f'format = "{view_format}"'))
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    _patch_checkout(
        monkeypatch,
        lambda _root, _resources: SimpleNamespace(
            commit="e" * 40, working_tree_dirty=False
        ),
    )
    plan = ip_release_planning.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )

    assert plan.record["exports"][0]["availability"] == {
        "simulation": True,
        "synthesis": synthesis,
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
    assert ip_packaging.resolve_release_view(
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
    _patch_checkout(
        monkeypatch,
        lambda _root, _resources: SimpleNamespace(
            commit="d" * 40, working_tree_dirty=False
        ),
    )
    with pytest.raises(ValueError, match="circuit pin order"):
        ip_release_planning.plan_ip_release_contract(
            contract,
            project=project,
        )


def test_native_oa_package_rejects_digital_interface_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    _patch_checkout(
        monkeypatch,
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
    interface_view = release_view(
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
    _patch_checkout(
        monkeypatch,
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
    circuit_view = release_view(
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

    _patch_checkout(
        monkeypatch,
        lambda _root, _resources: SimpleNamespace(
            commit="a" * 40, working_tree_dirty=False
        ),
    )

    plan = ip_release_planning.plan_ip_release_contract(
        contract,
        project=Project.open(tmp_path),
    )

    release_export = plan.payload.exports[0]
    assert release_export.name == "rtl-top"
    assert release_export.interface.kind == "rtl"
    assert release_export.interface.module == "rtl_top"
    assert release_export.availability.simulation
    assert not release_export.availability.physical_implementation
    implementation = ip_release_planning.plan_ip_release_contract(
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
        'module = "rtl_top"\nsource_view = "rtl_source"',
        'module = "rtl_alternate"\n'
        'source_view = "rtl_source"\n'
        'variant = "alternate"',
        1,
    ).replace('module = "rtl_top"', 'module = "rtl_alternate"', 1)
    contract_path.write_text(source, encoding="utf-8")
    _patch_checkout(
        monkeypatch,
        lambda _root, _resources: SimpleNamespace(
            commit="b" * 40, working_tree_dirty=False
        ),
    )

    contract = load_ip_contract(contract_path, project=Project.open(tmp_path))
    exported = contract.get_export("rtl-top")
    assert isinstance(exported.interface, RtlIpInterface)
    assert exported.interface.variant == "alternate"

    plan = ip_release_planning.plan_ip_release_contract(
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


def test_release_python_closure_includes_relative_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    owner = tmp_path / "ip/rtl_fixture"
    package = owner / "model"
    package.mkdir()
    (package / "__init__.py").write_text("from . import helper\n")
    (package / "helper.py").write_text("from .nested import value\n")
    (package / "nested.py").write_text("value = 7\n")
    program = owner / "entry.py"
    program.write_text("from .model import helper\n")
    component = owner / "configs/ip.toml"
    component.write_text(component.read_text().replace("[sources]", '[sources]\nmodel = "ip/rtl_fixture/entry.py"'))
    _patch_checkout(monkeypatch, lambda *_: SimpleNamespace(commit="a" * 40, working_tree_dirty=False))
    project = Project.open(tmp_path)
    plan = ip_release_planning.plan_ip_release_contract(load_ip_contract(contract_path, project=project), project=project)
    assert {"ip/rtl_fixture/entry.py", "ip/rtl_fixture/model/__init__.py", "ip/rtl_fixture/model/helper.py", "ip/rtl_fixture/model/nested.py"} <= set(plan.source_files)


def test_package_audit_recomputes_capability_from_views(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = _rtl_contract_fixture(tmp_path)
    _patch_checkout(monkeypatch, lambda *_: SimpleNamespace(commit="a" * 40, working_tree_dirty=False))
    project = Project.open(tmp_path)
    built = _publish_release(contract, project=project)
    manifest_path = _built_manifest(project, built)
    manifest = json.loads(manifest_path.read_text())
    for view in manifest["views"]:
        if view["role"] == "rtl_source":
            view["capabilities"] = ["diagnostic"]
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="availability"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def _commit_release_source(root: Path, message: str) -> str:
    (root / ".gitignore").write_text("artifacts/\n")
    git = shutil.which("git")
    if not (root / ".git").exists():
        subprocess.run([git, "init"], cwd=root, check=True, capture_output=True)
    subprocess.run([git, "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run([git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-m", message], cwd=root, check=True, capture_output=True)
    return subprocess.run([git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _set_view_applicability(contract: Path, applicability: dict[str, dict]) -> None:
    parts = contract.read_text().split("[[collateral]]")
    for index in range(1, len(parts)):
        name = tomllib.loads(parts[index])["name"]
        for field, value in applicability.get(name, {}).items():
            parts[index] = re.sub(rf"^{field} = .*\n", "", parts[index], flags=re.MULTILINE)
            if value is not None:
                if isinstance(value, dict):
                    fields = (f"{json.dumps(key)} = {json.dumps(item)}" for key, item in value.items())
                    encoded = "{ " + ", ".join(fields) + " }"
                else:
                    encoded = json.dumps(value)
                parts[index] += f"{field} = {encoded}\n"
    contract.write_text("[[collateral]]".join(parts))


def _signoff_contract_fixture(
    tmp_path: Path, *, evidence_fault: str | None = None, applicability: dict[str, dict] | None = None,
) -> tuple[Path, str]:
    contract_path = _native_oa_contract_fixture(tmp_path)
    configs = contract_path.parent
    sources = configs.parent / "sources"
    oa = {"library": "native_lib", "cell": "NATIVE_TOP", "schematic_view": "schematic", "layout_view": "layout"}
    views = {
        "raw_macro_lef": ("lef", ["physical_implementation"]),
        "raw_macro_liberty_or_db": ("liberty", ["synthesis", "physical_implementation", "characterized"]),
        "raw_macro_gds_or_oasis": ("gds", ["physical_implementation"]),
        "raw_macro_cdl_or_lvs_netlist": ("cdl", ["physical_implementation"]),
        "pex_netlist": ("spectre", ["circuit_simulation"]),
    }
    receipts = ("schematic_layout_parity_receipt", "drc_receipt", "lvs_receipt", "characterization_receipt")
    if evidence_fault == "structural-liberty":
        views["raw_macro_liberty_or_db"][1].remove("characterized")
    required = ["interface_contract", "oa_port_contract", "circuit_netlist"]
    text = contract_path.read_text()
    for level, extra in (("implementation", list(views)), ("signoff", [*views, *receipts])):
        text = text.replace(f"[exports.maturity.{level}]\nrequired_views = {json.dumps(required)}",
                            f"[exports.maturity.{level}]\nrequired_views = {json.dumps(required + extra)}")
    contract_path.write_text(text)
    views.update({role: ("json", ["signoff"]) for role in receipts})
    component = configs / "ip.toml"
    source_rows = []
    collateral_rows = []
    for role, (view_format, capabilities) in views.items():
        path = sources / f"{role}.data"
        path.write_text("{}" if role in receipts else "fixture view\n")
        source_rows.append(f'{role} = "{path.relative_to(tmp_path)}"')
        collateral_rows.append(f'''
[[collateral]]
export = "native-top"
name = "{role}"
role = "{role}"
component = "native-fixture"
source = "{role}"
package_path = "exports/native-top/{role}.data"
format = "{view_format}"
library = "native_lib"
cell = "NATIVE_TOP"
view = "layout"
condition = {{ corner = "tt" }}
capabilities = {json.dumps(capabilities)}
''')
    bindings = {
        "schematic_layout_parity_receipt": (["circuit_netlist", "raw_macro_gds_or_oasis", "raw_macro_cdl_or_lvs_netlist"], []),
        "drc_receipt": (["raw_macro_gds_or_oasis"], []),
        "lvs_receipt": (["circuit_netlist", "raw_macro_gds_or_oasis", "raw_macro_cdl_or_lvs_netlist"], []),
        "characterization_receipt": (["pex_netlist"], ["raw_macro_liberty_or_db"]),
    }
    if evidence_fault == "wrong-input-purpose":
        bindings["characterization_receipt"] = (["circuit_netlist"], ["raw_macro_liberty_or_db"])
    if evidence_fault == "missing-drc-purpose":
        collateral_rows = [row.replace('role = "drc_receipt"', 'role = "unrelated_receipt"') for row in collateral_rows]
    policies = "".join(f'[exports.receipts.{role}]\nauthority = "offline-fixture"\ninputs = {json.dumps(inputs)}\noutputs = {json.dumps(outputs)}\n'
                       for role, (inputs, outputs) in bindings.items())
    contract_path.write_text(contract_path.read_text().replace("[[collateral]]", policies + "\n[[collateral]]", 1))
    component.write_text(component.read_text().replace("[sources]", "[sources]\n" + "\n".join(source_rows)))
    contract_path.write_text(contract_path.read_text() + "".join(collateral_rows))
    operation = configs / "operations.toml"
    operation.write_text(operation.read_text().replace('maturity = "development"', 'maturity = "signoff"'))
    from sigilicon.adapters.release.release_semantics import ExportSemantics

    _set_view_applicability(contract_path, applicability or {})
    design_commit = _commit_release_source(tmp_path, "design and physical collateral")
    project = Project.open(tmp_path)
    contract = load_ip_contract(contract_path, project=project)
    plan = ip_release_planning.plan_ip_release_contract(contract, project=project, maturity="implementation")
    policy = ExportSemantics.from_source(contract.get_export("native-top"), "signoff", plan.payload.collateral)
    identities = {view.name: {"name": view.name, "size": view.size, "sha256": view.sha256}
                  for view in plan.payload.collateral}
    for role, (inputs, outputs) in bindings.items():
        receipt = {
            "schema": 4, "contract_kind": "release-receipt", "name": role, "status": "passed",
            "source_identity": policy.source_identity, "subject": {"kind": "circuit", **oa},
            "variant": None, "condition": {"corner": "tt"}, "coverage": [],
            "execution": {"kind": "external", "authority": "offline-fixture", "reference": "fixture-evidence-1"},
            "tool": {"name": "fixture", "version": "1"},
            "inputs": [identities[name] for name in inputs], "outputs": [identities[name] for name in outputs],
        }
        receipt.update((applicability or {}).get(role, {}))
        (sources / f"{role}.data").write_text(json.dumps(receipt))
    return contract_path, design_commit


@pytest.mark.parametrize("fault", ["structural-liberty", "wrong-input-purpose", "missing-drc-purpose"])
def test_native_signoff_requires_domain_evidence(tmp_path: Path, fault: str) -> None:
    contract_path, _ = _signoff_contract_fixture(tmp_path, evidence_fault=fault)
    _commit_release_source(tmp_path, "evidence with incomplete domain coverage")
    project = Project.open(tmp_path)
    contract = load_ip_contract(contract_path, project=project)
    plan = ip_release_planning.plan_ip_release_contract(contract, project=project, maturity="signoff")
    assert plan.missing_items
    assert project.preflight(project.plan(f"{contract.owner}:release")).ready is False


def test_release_selects_explicit_interface_view_among_same_role_views(tmp_path: Path) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    with contract_path.open("a") as stream:
        stream.write('''\n[[collateral]]
export = "rtl-top"
name = "another-interface"
role = "interface_contract"
component = "rtl-fixture"
source = "interface"
package_path = "exports/rtl-top/another-interface.toml"
format = "toml"
''')
    _commit_release_source(tmp_path, "ambiguous interface views")
    project = Project.open(tmp_path)
    assert project.preflight(project.plan("rtl-fixture:release")).ready


def _damage_receipt(receipt: dict, fault: str) -> None:
    if fault == "source-identity":
        receipt["source_identity"] = "0" * 64
    elif fault == "receipt-roles":
        receipt["inputs"] = []
    elif fault == "tool-version":
        receipt["tool"] = {"name": "fixture"}
    elif fault == "digest":
        receipt["inputs"][0]["sha256"] = "0" * 64
    elif fault == "size":
        receipt["inputs"][0]["size"] += 1
    elif fault == "direction":
        receipt["outputs"] = receipt["inputs"]
        receipt["inputs"] = []
    elif fault == "duplicate":
        receipt["inputs"].append(receipt["inputs"][0])
    elif fault == "status":
        receipt["status"] = "failed"
    elif fault == "subject":
        receipt["subject"] = {"kind": "rtl", "module": "wrong"}
    elif fault == "execution":
        receipt["execution"]["authority"] = "untrusted-fixture"
    elif fault == "condition":
        receipt["condition"] = {"corner": "wrong"}
    elif fault == "variant":
        receipt["variant"] = "wrong"
    elif fault == "coverage":
        receipt["coverage"] = ["unverified"]


def _rtl_signoff_fixture(tmp_path: Path, *, applicability: dict[str, dict] | None = None) -> tuple[Path, str]:
    from sigilicon.adapters.release.release_semantics import ExportSemantics

    contract_path = _rtl_contract_fixture(tmp_path)
    component = contract_path.parent / "ip.toml"
    receipts = ("synthesis_receipt", "physical_implementation_receipt")
    for role in receipts:
        path = component.parent.parent / f"{role}.json"
        path.write_text("{}")
        component.write_text(component.read_text().replace(
            "[sources]", f'[sources]\n{role} = "{path.relative_to(tmp_path)}"'))
        with contract_path.open("a") as stream:
            stream.write(f'''\n[[collateral]]
export = "rtl-top"
name = "{role}"
role = "{role}"
component = "rtl-fixture"
source = "{role}"
package_path = "exports/rtl-top/{role}.json"
format = "json"
capabilities = ["signoff"]
''')
    operation = component.parent / "operations.toml"
    operation.write_text(operation.read_text().replace('maturity = "development"', 'maturity = "signoff"'))
    _set_view_applicability(contract_path, applicability or {})
    design_commit = _commit_release_source(tmp_path, "RTL source with evidence policy")
    project = Project.open(tmp_path)
    contract = load_ip_contract(contract_path, project=project)
    plan = ip_release_planning.plan_ip_release_contract(contract, project=project)
    policy = ExportSemantics.from_source(contract.get_export("rtl-top"), "signoff", plan.payload.collateral)
    inputs = [{"name": view.name, "size": view.size, "sha256": view.sha256}
              for view in plan.payload.collateral if view.role in {"interface_contract", "rtl_source"}]
    for role in receipts:
        receipt = {
            "schema": 4, "contract_kind": "release-receipt", "name": role, "status": "passed",
            "source_identity": policy.source_identity, "subject": {"kind": "rtl", "module": "rtl_top"},
            "variant": None, "condition": {}, "coverage": [],
            "execution": {"kind": "external", "authority": "offline-fixture", "reference": "fixture-evidence-1"},
            "tool": {"name": "fixture", "version": "1"}, "inputs": inputs, "outputs": [],
        }
        receipt.update((applicability or {}).get(role, {}))
        (component.parent.parent / f"{role}.json").write_text(json.dumps(receipt))
    return contract_path, design_commit


@pytest.mark.parametrize("rtl,receipt_name,view_name", [
    (False, "schematic_layout_parity_receipt", "raw_macro_cdl_or_lvs_netlist"),
    (False, "drc_receipt", "raw_macro_gds_or_oasis"),
    (False, "lvs_receipt", "raw_macro_gds_or_oasis"),
    (False, "characterization_receipt", "pex_netlist"),
    (False, "characterization_receipt", "raw_macro_liberty_or_db"),
    (True, "synthesis_receipt", "rtl_source"),
    (True, "physical_implementation_receipt", "rtl_source"),
])
@pytest.mark.parametrize("dimension", ["variant", "condition"])
def test_signoff_rejects_conflicting_bound_view_applicability(
    tmp_path: Path, rtl: bool, receipt_name: str, view_name: str, dimension: str,
) -> None:
    applicability = {
        view_name: {dimension: "low-voltage" if dimension == "variant" else {"corner": "ff"}},
        receipt_name: {dimension: "nominal" if dimension == "variant" else {"corner": "tt"}},
    }
    contract_path, _ = (_rtl_signoff_fixture if rtl else _signoff_contract_fixture)(tmp_path, applicability=applicability)
    _commit_release_source(tmp_path, "conflicting evidence applicability")
    project = Project.open(tmp_path)
    contract = load_ip_contract(contract_path, project=project)
    plan = ip_release_planning.plan_ip_release_contract(contract, project=project, maturity="signoff")
    assert any(receipt_name in problem and "applicability-conflict" in problem for problem in plan.missing_items)
    assert not project.preflight(project.plan(f"{contract.owner}:release")).ready


@pytest.mark.parametrize("applicability", [
    {"raw_macro_gds_or_oasis": {"variant": "low-voltage"}},
    {"drc_receipt": {"variant": "nominal"}},
    {"drc_receipt": {"condition": {"corner": "tt", "voltage": 0.9}}},
    {"drc_receipt": {"condition": {"voltage": 0.9}}},
    {"characterization_receipt": {"variant": "nominal", "condition": {"corner": "tt", "voltage": 0.9}}},
])
def test_native_signoff_preserves_shared_and_partially_qualified_views(tmp_path: Path, applicability: dict) -> None:
    contract_path, _ = _signoff_contract_fixture(tmp_path, applicability=applicability)
    _commit_release_source(tmp_path, "compatible evidence applicability")
    project = Project.open(tmp_path)
    built = _publish_release(contract_path, project=project)
    audited = ip_packaging.audit_ip_release_manifest(_built_manifest(project, built))
    assert audited["availability"]["physical_implementation"] is True


@pytest.mark.parametrize("rtl", [False, True], ids=["circuit", "rtl"])
def test_package_audit_rejects_conflicting_receipt_applicability(tmp_path: Path, rtl: bool) -> None:
    input_name = "rtl_source" if rtl else "raw_macro_gds_or_oasis"
    receipt_name = "synthesis_receipt" if rtl else "drc_receipt"
    contract_path, _ = (_rtl_signoff_fixture if rtl else _signoff_contract_fixture)(
        tmp_path, applicability={input_name: {"variant": "low-voltage"}},
    )
    _commit_release_source(tmp_path, "shared evidence applicability")
    project = Project.open(tmp_path)
    manifest_path = _built_manifest(project, _publish_release(contract_path, project=project))
    manifest = json.loads(manifest_path.read_text())
    view = next(row for row in manifest["views"] if row["name"] == receipt_name)
    path = manifest_path.parent / view["path"]
    receipt = json.loads(path.read_text())
    receipt["variant"] = view["variant"] = "nominal"
    path.chmod(0o644)
    path.write_text(json.dumps(receipt))
    view["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    view["size"] = path.stat().st_size
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="applicability-conflict"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


@pytest.mark.parametrize("rtl", [False, True], ids=["circuit", "rtl"])
@pytest.mark.parametrize("receipt_fault", [None, "source-identity", "receipt-roles", "tool-version", "digest", "size", "direction", "duplicate", "status", "subject", "execution", "condition", "variant", "coverage"])
def test_signoff_receipt_policy_round_trip(tmp_path: Path, receipt_fault: str | None, rtl: bool) -> None:
    contract_path, design_commit = (_rtl_signoff_fixture if rtl else _signoff_contract_fixture)(tmp_path)
    receipt_path = contract_path.parent.parent / ("synthesis_receipt.json" if rtl else "sources/drc_receipt.data")
    if receipt_fault:
        receipt = json.loads(receipt_path.read_text())
        _damage_receipt(receipt, receipt_fault)
        receipt_path.write_text(json.dumps(receipt))
    release_commit = _commit_release_source(tmp_path, "record evidence for the verified design")
    assert release_commit != design_commit
    project = Project.open(tmp_path)
    contract = load_ip_contract(contract_path, project=project)
    plan = ip_release_planning.plan_ip_release_contract(contract, project=project, maturity="signoff")
    if receipt_fault:
        assert plan.missing_items
        assert plan.record["availability"]["physical_implementation"] is False
        assert project.preflight(project.plan(f"{contract.owner}:release")).ready is False
    else:
        assert plan.missing_items == ()
        built = _publish_release(contract_path, project=project, maturity="signoff")
        audited = ip_packaging.audit_ip_release_manifest(_built_manifest(project, built))
        assert audited["availability"]["physical_implementation"] is True
        assert audited["source_commit"] == release_commit


@pytest.mark.parametrize("rtl", [False, True], ids=["circuit", "rtl"])
@pytest.mark.parametrize("fault", ["digest", "source-identity", "direction", "status", "subject", "execution", "condition", "variant", "coverage"])
def test_package_audit_validates_receipt_content_bindings(tmp_path: Path, fault: str, rtl: bool) -> None:
    contract_path, _ = (_rtl_signoff_fixture if rtl else _signoff_contract_fixture)(tmp_path)
    _commit_release_source(tmp_path, "signoff evidence")
    project = Project.open(tmp_path)
    built = _publish_release(contract_path, project=project)
    manifest_path = _built_manifest(project, built)
    manifest = json.loads(manifest_path.read_text())
    view = next(row for row in manifest["views"] if row["role"] == ("synthesis_receipt" if rtl else "drc_receipt"))
    path = manifest_path.parent / view["path"]
    receipt = json.loads(path.read_text())
    _damage_receipt(receipt, fault)
    path.chmod(0o644)
    path.write_text(json.dumps(receipt))
    view["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    view["size"] = path.stat().st_size
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="qualified view semantics"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


@pytest.mark.parametrize("source_fault", ["ignored", "skip-worktree"])
def test_release_requires_each_source_to_match_its_git_blob(tmp_path: Path, source_fault: str) -> None:
    _rtl_contract_fixture(tmp_path)
    source = "ip/rtl_fixture/rtl/top.sv"
    commit = _commit_release_source(tmp_path, "release sources")
    git = shutil.which("git")
    if source_fault == "ignored":
        subprocess.run([git, "rm", "--cached", source], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / ".gitignore").write_text("artifacts/\n" + source + "\n")
        subprocess.run([git, "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run([git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-m", "ignore RTL"], cwd=tmp_path, check=True, capture_output=True)
    else:
        subprocess.run([git, "update-index", "--skip-worktree", source], cwd=tmp_path, check=True, capture_output=True)
        with (tmp_path / source).open("a") as stream:
            stream.write("// local uncommitted source\n")
    assert subprocess.run([git, "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout == ""
    project = Project.open(tmp_path)
    with pytest.raises(RuntimeError, match="Git commit|tracked by"):
        project.run(project.plan("rtl-fixture:release"))
    assert not list((tmp_path / "artifacts/release-store").glob("*/objects/sha256-*"))


def test_managed_release_rechecks_the_real_git_commit(tmp_path: Path) -> None:
    contract_path = _rtl_contract_fixture(tmp_path)
    (tmp_path / ".gitignore").write_text("artifacts/\n")
    git = shutil.which("git")
    for arguments in (("init",), ("add", "."),
                      ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "source")):
        subprocess.run([git, *arguments], cwd=tmp_path, check=True, capture_output=True)
    commit = subprocess.run([git, "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    project = Project.open(tmp_path)
    built = _publish_release(contract_path, project=project)
    audited = ip_packaging.audit_ip_release_manifest(_built_manifest(project, built))
    assert audited["source_commit"] == commit
    assert audited["availability"]["simulation"] is True


def test_rtl_signoff_rejects_failed_receipts(tmp_path: Path) -> None:
    contract = _rtl_contract_fixture(tmp_path)
    component = contract.parent / "ip.toml"
    for role in ("synthesis_receipt", "physical_implementation_receipt"):
        source = contract.parent.parent / f"{role}.json"
        source.write_text('{"status":"failed","source_identity":"wrong"}\n')
        component.write_text(component.read_text().replace(
            "[sources]", f'[sources]\n{role} = "{source.relative_to(tmp_path)}"'
        ))
        with contract.open("a") as stream:
            stream.write(f'''\n[[collateral]]
export = "rtl-top"
name = "{role}"
role = "{role}"
component = "rtl-fixture"
source = "{role}"
package_path = "exports/rtl-top/{role}.json"
format = "json"
''')
    operations = contract.parent / "operations.toml"
    operations.write_text(operations.read_text().replace(
        'maturity = "development"', 'maturity = "signoff"'
    ))
    _commit_release_source(tmp_path, "RTL source and failed evidence")
    project = Project.open(tmp_path)
    assert not project.preflight(project.plan("rtl-fixture:release")).ready


def test_release_publication_does_not_change_its_input_identity(tmp_path: Path) -> None:
    contract = _rtl_contract_fixture(tmp_path)
    _commit_release_source(tmp_path, "RTL release source")
    project = Project.open(tmp_path)
    before = project.plan("rtl-fixture:release")
    built = _publish_release(contract, project=project)
    assert ip_packaging.audit_ip_release_manifest(_built_manifest(project, built))["source_commit"]
    after = Project.open(tmp_path).plan("rtl-fixture:release")
    assert after.identity == before.identity
    store = project.resources().require_destination("release-store.rtl-fixture")
    (store / "unrelated").symlink_to(tmp_path / "missing-payload")
    assert Project.open(tmp_path).plan("rtl-fixture:release").identity == before.identity
    assert project.run(before).status == "succeeded"


def test_release_rejects_replaced_destination(tmp_path: Path) -> None:
    _rtl_contract_fixture(tmp_path)
    _commit_release_source(tmp_path, "RTL release source")
    project = Project.open(tmp_path)
    plan = project.plan("rtl-fixture:release")
    store = project.resources().require_destination("release-store.rtl-fixture")
    store.rename(store.with_name("replaced-store"))
    store.mkdir()
    assert not project.preflight(plan).ready


def test_native_release_preserves_multiple_timing_corners(tmp_path: Path) -> None:
    from sigilicon.release_store import audit_release_package
    from sigilicon.release_views import ViewSelector

    contract = _native_oa_contract_fixture(tmp_path)
    component = contract.parent / "ip.toml"
    for variant in ("nominal", "low-voltage"):
        for corner in ("ss", "ff"):
            for kind, role, view_format in (("liberty", "raw_macro_liberty_or_db", "liberty"),
                                             ("pex", "pex_netlist", "spectre")):
                name = f"{kind}-{variant}-{corner}"
                source = contract.parent.parent / "sources" / f"{name}.data"
                source.write_text(f"{name} fixture view\n")
                component.write_text(component.read_text().replace(
                    "[sources]", f'[sources]\n{name} = "{source.relative_to(tmp_path)}"'))
                with contract.open("a") as stream:
                    stream.write(f'''\n[[collateral]]
export = "native-top"
name = "{name}"
role = "{role}"
component = "native-fixture"
source = "{name}"
package_path = "exports/native-top/{name}.data"
format = "{view_format}"
library = "native_lib"
cell = "NATIVE_TOP"
view = "{kind}"
variant = "{variant}"
condition = {{ process = "{corner}" }}
capabilities = ["synthesis", "physical_implementation", "circuit_simulation"]
''')
    _commit_release_source(tmp_path, "multi-condition source release")
    project = Project.open(tmp_path)
    built = _publish_release(contract, project=project)
    manifest = _built_manifest(project, built)
    ip_packaging.audit_ip_release_manifest(manifest)
    package = audit_release_package(manifest)
    for role, kind in (("raw_macro_liberty_or_db", "liberty"), ("pex_netlist", "pex")):
        for variant in ("nominal", "low-voltage"):
            for corner in ("ss", "ff"):
                artifact = package.select("native-top", ViewSelector(role, variant, {"process": corner}))
                assert artifact.name == f"{kind}-{variant}-{corner}"
        for selector in (ViewSelector(role), ViewSelector(role, "missing", {"process": "ss"}),
                         ViewSelector(role, "nominal", {"process": "tt"})):
            with pytest.raises(RuntimeError, match="missing or ambiguous"):
                package.select("native-top", selector)
    artifact = package.view("native-top", "liberty-nominal-ss")
    artifact.path.chmod(0o644)
    artifact.path.write_text("tampered\n")
    with pytest.raises(RuntimeError, match="content changed"):
        package.select("native-top", ViewSelector("raw_macro_liberty_or_db", "nominal", {"process": "ss"}))


def test_native_circuit_release_keeps_conditioned_views_and_canonical_binding(tmp_path: Path) -> None:
    contract_path = _native_oa_contract_fixture(tmp_path)
    with contract_path.open("a") as stream:
        stream.write('''\n[[collateral]]
export = "native-top"
name = "circuit_ff"
role = "circuit_netlist"
component = "native-fixture"
source = "circuit"
package_path = "exports/native-top/ff.scs"
format = "spectre-source"
condition = {corner = "ff"}
capabilities = ["circuit_simulation"]
''')
    _commit_release_source(tmp_path, "native circuit with multiple conditions")
    project = Project.open(tmp_path)
    built = _publish_release(contract_path, project=project)
    manifest = _built_manifest(project, built)
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    circuit = ip_packaging.resolve_release_view(audited, manifest, "circuit_netlist", export="native-top")
    assert "subckt NATIVE_CHILD" in circuit.read_text()
    alternate = ip_packaging.release_view(audited, "circuit_ff", export="native-top")
    assert alternate["condition"] == {"corner": "ff"}


def test_generated_release_consumes_closed_run_without_tracking_outputs(tmp_path: Path, monkeypatch) -> None:
    from sigilicon.execution import AdapterPreparation
    from sigilicon.execution._result import Artifact, StepResult
    from sigilicon.release_store import ReleaseRef, ReleaseStore
    from sigilicon.adapters import trusted_adapters

    contract = _rtl_contract_fixture(tmp_path)
    operations = contract.parent / "operations.toml"
    component = contract.parent / "ip.toml"
    component.write_text(component.read_text().replace('[filesets]', '[filesets]\ndesign = ["rtl"]'))
    with operations.open("a") as stream:
        stream.write('''\n[operations.generate]
uses = "fake.generate"
filesets = [{component = "rtl-fixture", fileset = "design"}]
''')

    class Generate:
        name = "fake.generate"

        def prepare(self, project, step, resources):
            return AdapterPreparation()

        def preflight(self, step, resources):
            return ()

        def run(self, context):
            path = context.write_text("report", "generated.txt", "generated diagnostic\n")
            return StepResult.succeeded(artifacts=(Artifact("report", "text.plain", path),))

    adapters = (*trusted_adapters(), Generate())
    monkeypatch.setattr("sigilicon.adapters.trusted_adapters", lambda: adapters)
    _commit_release_source(tmp_path, "source package and producer operation")
    project = Project.open(tmp_path)
    base = _publish_release(contract, project=project)
    run = project.run(project.plan("rtl-fixture:generate"))
    with operations.open("a") as stream:
        stream.write(f'''\n[operations.package]
uses = "sigilicon.release-artifacts"
filesets = [{{component = "rtl-fixture", fileset = "release"}}]
[operations.package.config]
base = {{store = "{base['store']}", manifest_sha256 = "{base['manifest_sha256']}"}}
run = {{owner = "rtl-fixture", operation = "generate", run_id = "{run.run_id}"}}
views = [{{export = "rtl-top", name = "diagnostic", role = "diagnostic", format = "text", package_path = "exports/rtl-top/diagnostic.txt", artifact = {{step = "run", role = "report", kind = "text.plain"}}}}]
''')
    _commit_release_source(tmp_path, "bind generated artifact publication")
    project = Project.open(tmp_path)
    result = project.run(project.plan("rtl-fixture:package"))
    summary = json.loads(result.outcomes[0].result.artifacts[0].read_text())
    package = ReleaseStore(tmp_path / "artifacts/release-store").open(
        ReleaseRef(summary["store"], summary["manifest_sha256"]),
        validate=ip_packaging.validate_ip_release_package,
    )
    assert package.manifest["release_kind"] == "build-artifact-package"
    assert package.view("rtl-top", "diagnostic").path.read_text() == "generated diagnostic\n"
    assert package.manifest["source_commit"] == base["source_commit"]
    assert package.manifest["provenance"]["build"]["execution"]["result"]["run_id"] == run.run_id


def test_netlist_only_circuit_publishes_without_oa_or_a_pdk(tmp_path: Path) -> None:
    contract = _rtl_contract_fixture(tmp_path)
    owner = contract.parent.parent
    (owner / "circuit.scs").write_text("subckt AMP IN OUT VSS\nR0 (IN OUT) resistor r=1k\nends AMP\n")
    (contract.parent / "interface.toml").write_text('''schema = 1
contract_kind = "circuit-interface"
path_scope = "owner"
owner = "rtl-fixture"
[circuit]
top = "AMP"
ports = ["IN", "OUT", "VSS"]
primitive_masters = ["resistor"]
''')
    component = contract.parent / "ip.toml"
    component.write_text(component.read_text().replace('[sources]', '[sources]\ncircuit = "ip/rtl_fixture/circuit.scs"'))
    contract.write_text('''schema = 5
contract_kind = "ip-release"
path_scope = "owner"
owner = "rtl-fixture"
name = "rtl-fixture"
default_maturity = "development"
[[exports]]
name = "amplifier"
[exports.interface]
kind = "circuit"
top = "AMP"
contract = "configs/interface.toml"
bindings = {interface_contract = "pins", circuit_netlist = "spectre"}
[exports.maturity.development]
required_views = ["pins", "spectre"]
[exports.maturity.implementation]
required_views = ["pins", "spectre", "abstract"]
[exports.maturity.signoff]
required_views = ["pins", "spectre", "abstract"]
[[collateral]]
export = "amplifier"
name = "pins"
role = "interface_contract"
component = "rtl-fixture"
source = "interface"
package_path = "exports/amplifier/interface.toml"
format = "toml"
[[collateral]]
export = "amplifier"
name = "spectre"
role = "circuit_netlist"
component = "rtl-fixture"
source = "circuit"
package_path = "exports/amplifier/amp.scs"
format = "spectre-source"
capabilities = ["circuit_simulation"]
''')
    _commit_release_source(tmp_path, "netlist only circuit")
    project = Project.open(tmp_path)
    parsed = load_ip_contract(contract, project=project).get_export("amplifier").interface
    assert isinstance(parsed, CircuitIpInterface)
    assert parsed.top == "AMP" and parsed.authoring is None
    built = _publish_release(contract, project=project)
    manifest = _built_manifest(project, built)
    audited = ip_packaging.audit_ip_release_manifest(manifest)
    assert audited["exports"][0]["interface"]["top"] == "AMP"
    payload = json.loads(manifest.read_text())
    assert payload["exports"][0]["availability"]["simulation"]
    (owner / "circuit.scs").write_text("subckt AMP OUT IN VSS\nends AMP\n")
    with pytest.raises(ValueError, match="pin order"):
        Project.open(tmp_path).plan("rtl-fixture:release")


def test_native_authored_circuit_release_accepts_ports_without_a_design_generator(tmp_path: Path) -> None:
    from sigilicon.domain.oa_snapshot import NativeOaSnapshot
    from sigilicon.domain.platform import load_platform
    from sigilicon.adapters.cadence.oa_library import plan_oa_library_rebuild

    contract_path = _native_oa_contract_fixture(tmp_path)
    project = Project.open(tmp_path)
    technology = load_platform(project, "testpdk", resources=project.resources()).oa.technology_library
    for cell_path in (tmp_path / "ip/native_fixture/sources").glob("*/cell.toml"):
        text = cell_path.read_text()
        for view in ("schematic", "symbol"):
            snapshot = NativeOaSnapshot("native_lib", cell_path.parent.name, view, technology,
                                        {f"{view}.oa": b"offline native OA boundary fixture"})
            (cell_path.parent / f"{view}.json").write_text(json.dumps(snapshot.record))
            text = text.replace(f'kind = "{view}", source = "design.toml"', f'kind = "native_oa", source = "{view}.json"')
        cell_path.write_text(text)
    _commit_release_source(tmp_path, "native authoring source")
    project = Project.open(tmp_path)
    oa_manifest = contract_path.parent / "oa.toml"
    oa_plan = plan_oa_library_rebuild(oa_manifest, project=project)
    contract = load_ip_contract(contract_path, project=project)
    release = ip_release_planning.plan_ip_release_contract(contract, project=project, oa_source_inventory={oa_manifest: oa_plan.source}, oa_plan_inventory={oa_manifest: oa_plan})
    assert release.record["missing_items"] == []
    assert release.payload.exports[0].availability.simulation
