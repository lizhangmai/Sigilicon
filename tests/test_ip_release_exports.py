from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import sigilicon.domain.component as component_domain
import sigilicon.domain.oa_library as oa_library_domain
import sigilicon.domain.oa_simulation as oa_simulation_domain
import sigilicon.domain.platform as platform_domain
import sigilicon.workflows.ip_packaging as ip_packaging
from sigilicon.domain.ip_release import load_ip_contract
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
''',
        encoding="utf-8",
    )
    return contract


def test_one_ip_contract_exposes_multiple_scoped_circuits(tmp_path: Path) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project_root=tmp_path)

    assert contract.name == "fixture-ip"
    assert contract.owner == "fixture"
    assert [item.name for item in contract.exports] == ["left", "right"]
    assert contract.get_export("left").oa_cell == "LEFT"
    assert contract.get_export("right").oa_cell == "RIGHT"
    assert [item.role for item in contract.collateral] == [
        "interface_contract",
        "interface_contract",
    ]


def test_ip_contract_reuses_explicit_project(tmp_path: Path) -> None:
    contract_path = _contract_fixture(tmp_path)
    project = Project.from_project_root(tmp_path)

    contract = load_ip_contract(contract_path, project=project)

    assert contract.project is project
    assert contract.project_root == tmp_path
    assert contract.component_graph == {"fixture-ip": project.owner("fixture").component}
    assert contract.component_graph["fixture-ip"] is project.owner("fixture").component
    assert contract.document["name"] == "fixture-ip"
    with pytest.raises(TypeError):
        contract.document["exports"][0]["name"] = "other"


def test_release_consumers_reuse_the_contract_component_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project_root=tmp_path)

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
    for path in (release, assembly, cell_manifest, setup):
        path.write_text("name = 'fixture'\n", encoding="utf-8")
    netlist.write_text("subckt TOP A\nends TOP\n", encoding="utf-8")
    top = SimpleNamespace(
        cell="TOP",
        owner="fixture",
        role="design",
        canonical_source=netlist,
        source_manifest_path=assembly,
        manifest_path=cell_manifest,
        design_spec=None,
        layout_specs=(),
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
        exports=(SimpleNamespace(oa_library="fixture", oa_cell="TOP"),),
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

    with pytest.raises(
        ValueError,
        match="platform inventory has no 'testpdk' entry",
    ):
        ip_packaging._source_inputs(contract, platform_inventory={})


def test_ip_contract_owner_must_match_cataloged_owner(tmp_path: Path) -> None:
    contract_path = _contract_fixture(tmp_path)
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8").replace(
            'owner = "fixture"', 'owner = "other"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="owner"):
        load_ip_contract(contract_path, project_root=tmp_path)


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
        load_ip_contract(contract_path, project_root=tmp_path)


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
        load_ip_contract(contract_path, project_root=tmp_path)


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
        load_ip_contract(contract_path, project_root=tmp_path)
