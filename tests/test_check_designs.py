from __future__ import annotations

from collections import UserDict
from dataclasses import replace
from pathlib import Path
import tomllib
from types import MappingProxyType, SimpleNamespace

import pytest

from conftest import (
    write_component_owner,
    write_test_layout_platform,
    write_test_platform,
)
import sigilicon.domain.repository as repository_module
from sigilicon.cli.check_designs import main as check_designs_main
from sigilicon.domain.config_contracts import freeze_toml_document
from sigilicon.domain.repository import Project
import sigilicon.workflows.repository_checks as repository_checks


def _write_release_target(root: Path) -> Path:
    owner = root / "ip/fixture"
    interface = owner / "interface.toml"
    write_component_owner(
        root,
        "fixture",
        filesets={"interface": ("ip/fixture/interface.toml",)},
    )
    interface.write_text(
        '''schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "fixture"
''',
        encoding="utf-8",
    )
    (owner / "oa.toml").write_text(
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "fixture"
name = "fixture"
''',
        encoding="utf-8",
    )
    release = owner / "release.toml"
    release.write_text(
        '''schema = 1
contract_kind = "ip-release"
path_scope = "owner"
owner = "fixture"

name = "fixture"
producer = "ip/fixture"
component = "component.toml"
default_maturity = "development"

[[exports]]
name = "macro"
[exports.oa]
library = "fixture"
cell = "FIXTURE"
schematic_view = "schematic"
layout_view = "layout"
[exports.interface]
contract = "interface.toml"
physical = "FIXTURE:physical"
logical = "fixture_model:logical"
[exports.maturity.development]
required_roles = ["interface_contract"]
[exports.maturity.implementation]
required_roles = ["interface_contract"]
[exports.maturity.signoff]
required_roles = ["interface_contract"]

[[collateral]]
export = "macro"
role = "interface_contract"
component = "fixture"
fileset = "interface"
package_path = "exports/macro/interface.toml"
format = "toml"

[source]
oa_assembly = "ip/fixture/oa.toml"
files = []
''',
        encoding="utf-8",
    )
    catalog = root / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + '''
[targets.fixture]
contract = "ip/fixture/release.toml"
''',
        encoding="utf-8",
    )
    return release


def test_check_designs_parses_the_project_manifest_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract = (tmp_path / "sigilicon.toml").resolve()
    original = repository_module.read_toml
    original_load = tomllib.load
    manifest_reads = 0
    toml_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == contract:
            manifest_reads += 1
        return original(path)

    def counted_load(stream):
        nonlocal toml_reads
        if Path(stream.name).resolve() == contract:
            toml_reads += 1
        return original_load(stream)

    monkeypatch.setattr(repository_module, "read_toml", counted)
    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert manifest_reads == 1
    assert toml_reads == 1
    assert '"passed": true' in capsys.readouterr().out


def test_project_manifest_source_document_is_frozen_and_resolved(
    tmp_path: Path,
) -> None:
    project = Project.from_project_root(tmp_path)

    assert project.manifest_source_document() is project.manifest_document
    with pytest.raises(TypeError):
        project.manifest_document["catalogs"]["ip"] = "other.toml"
    with pytest.raises(ValueError, match="source document drift"):
        replace(
            project,
            manifest_document=dict(project.manifest_document),
        ).manifest_source_document()

    drifted = dict(project.manifest_document)
    drifted["catalogs"] = {
        **project.manifest_document["catalogs"],
        "ip": "configs/platform/catalog.toml",
    }
    with pytest.raises(ValueError, match="catalog|source document drift"):
        replace(
            project,
            manifest_document=freeze_toml_document(drifted),
        ).manifest_source_document()

    run_scoped = project.with_artifact_root(tmp_path / "run-artifacts")
    assert run_scoped.manifest_source_document() is project.manifest_document
    path_drift = dict(project.manifest_document)
    path_drift["paths"] = {
        **project.manifest_document["paths"],
        "artifact_root": "other-artifacts",
    }
    with pytest.raises(ValueError, match="identity drift"):
        replace(
            project,
            manifest_document=freeze_toml_document(path_drift),
        ).manifest_source_document()


def test_check_designs_reads_the_ip_catalog_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = (tmp_path / "catalogs/ip.toml").resolve()
    original_load = tomllib.load
    reads = 0

    def counted_load(stream):
        nonlocal reads
        if Path(stream.name).resolve() == catalog:
            reads += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert reads == 1


def test_check_designs_accepts_an_omitted_empty_ip_targets_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace("[targets]\n", ""),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0


def test_check_designs_reuses_the_loaded_component_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = write_component_owner(tmp_path, "example", filesets={}).resolve()
    original_load = tomllib.load
    component_reads = 0

    def counted_load(stream):
        nonlocal component_reads
        if Path(stream.name).resolve() == component:
            component_reads += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert component_reads == 1


def test_architecture_inventory_preserves_typed_variant_sources_for_reuse(
    tmp_path: Path,
) -> None:
    behavior = tmp_path / "ip/example/configs/behavior.toml"
    variant = tmp_path / "ip/example/configs/variant.toml"
    behavior.parent.mkdir(parents=True)
    behavior.write_text(
        '''schema = 1
contract_kind = "ip-architecture-behavior"
path_scope = "owner"
owner = "example"
''',
        encoding="utf-8",
    )
    variant.write_text(
        '''schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "example"
''',
        encoding="utf-8",
    )
    component = write_component_owner(
        tmp_path,
        "example",
        filesets={
            "architecture": (
                "ip/example/configs/behavior.toml",
                "ip/example/configs/variant.toml",
            )
        },
    )
    component.write_text(
        component.read_text(encoding="utf-8")
        + '\n[variants]\ndefault = "ip/example/configs/variant.toml"\n',
        encoding="utf-8",
    )

    project = Project.from_project_root(tmp_path)
    documents = repository_checks._architecture_source_documents(project)
    variants = repository_checks._integration_variant_inventory(
        project,
        component.resolve(),
        documents,
    )

    assert set(documents) == {behavior.resolve(), variant.resolve()}
    assert variants is not None
    assert set(variants) == {variant.resolve()}
    assert variants[variant.resolve()] is documents[variant.resolve()]
    with pytest.raises(TypeError):
        documents[behavior.resolve()]["schema"] = 2


def test_architecture_inventory_rejects_owner_drift(
    tmp_path: Path,
) -> None:
    architecture = tmp_path / "ip/example/configs/behavior.toml"
    architecture.parent.mkdir(parents=True)
    architecture.write_text(
        '''schema = 1
contract_kind = "ip-architecture-behavior"
path_scope = "owner"
owner = "other"
''',
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "architecture": ("ip/example/configs/behavior.toml",),
        },
    )

    with pytest.raises(ValueError, match="owner must be 'example'"):
        repository_checks._architecture_source_documents(
            Project.from_project_root(tmp_path)
        )


def test_architecture_inventory_rejects_cross_owner_sources(
    tmp_path: Path,
) -> None:
    architecture = tmp_path / "ip/other/configs/behavior.toml"
    architecture.parent.mkdir(parents=True)
    architecture.write_text("name = 'native'\n", encoding="utf-8")
    write_component_owner(tmp_path, "other", filesets={})
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "architecture": ("ip/other/configs/behavior.toml",),
        },
    )

    with pytest.raises(ValueError, match="cataloged root|architecture fileset source"):
        repository_checks._architecture_source_documents(
            Project.from_project_root(tmp_path)
        )


def test_check_designs_reads_each_ip_release_contract_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _write_release_target(tmp_path).resolve()
    reads = 0
    original_load = tomllib.load

    def counted_load(stream):
        nonlocal reads
        if Path(stream.name).resolve() == release:
            reads += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.setattr(
        repository_checks,
        "plan_oa_library_rebuild",
        lambda *_args, **_kwargs: SimpleNamespace(
            as_dict=lambda: {},
            designs=(),
            layouts=(),
            testbenches=(),
        ),
    )
    monkeypatch.setattr(
        repository_checks,
        "load_oa_library_source",
        lambda path, *, project: SimpleNamespace(
            manifest_path=path.resolve(),
            project=project,
            source_roots=(
                SimpleNamespace(
                    manifest_path=path.resolve(),
                    owner="fixture",
                ),
            ),
            source_documents={},
        ),
    )
    monkeypatch.setattr(
        "sigilicon.domain.oa_library.resolve_oa_library_source",
        lambda _path, *, project, snapshot: snapshot,
    )
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert reads == 1


def test_check_designs_reads_shared_flow_catalog_inventory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_root = tmp_path / "ip/example"
    flow_root = owner_root / "configs/flows"
    flow_root.mkdir(parents=True)
    catalogs = (
        flow_root / "design_targets.toml",
        flow_root / "layout_targets.toml",
    )
    catalogs[0].write_text(
        '''schema = 1
contract_kind = "flow-design-registry"
path_scope = "owner"
owner = "example"

[targets]
''',
        encoding="utf-8",
    )
    catalogs[1].write_text(
        '''schema = 1
contract_kind = "flow-layout-registry"
path_scope = "owner"
owner = "example"

[targets]
''',
        encoding="utf-8",
    )
    shared_sources = (
        (flow_root / "flow.toml", "flow"),
        (flow_root / "profile.toml", "execution-profile"),
        (
            owner_root / "configs/physical_materialization/target.toml",
            "physical-materialization-target",
        ),
    )
    for path, contract_kind in shared_sources:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'''schema = 1
contract_kind = "{contract_kind}"
path_scope = "owner"
owner = "example"

[payload]
values = ["fixture"]
''',
            encoding="utf-8",
        )
    selected_sources = (*catalogs, *(path for path, _kind in shared_sources))
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": tuple(
                path.relative_to(tmp_path).as_posix() for path in selected_sources
            )
        },
    )
    catalog_paths = {path.resolve() for path in selected_sources}
    reads = {path: 0 for path in catalog_paths}
    original_load = tomllib.load

    def counted_load(stream):
        path = Path(stream.name).resolve()
        if path in reads:
            reads[path] += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert set(reads.values()) == {1}


def test_flow_source_inventory_is_frozen_and_catalog_filtered(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "ip/example"
    flow_root = owner_root / "configs/flows"
    flow_root.mkdir(parents=True)
    catalog = flow_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows]
''',
        encoding="utf-8",
    )
    source = flow_root / "source.toml"
    source.write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "example"

[payload]
values = ["fixture"]
''',
        encoding="utf-8",
    )
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": tuple(
                path.relative_to(tmp_path).as_posix() for path in (catalog, source)
            )
        },
    )
    project = Project.from_project_root(tmp_path)
    inventory = project.flow_catalog_inventory()

    assert {snapshot.contract_kind for snapshot in inventory} == {
        "flow-catalog",
        "source-assets",
    }
    assert tuple(
        snapshot.contract_kind
        for snapshot in project.owner_flow_catalog_snapshots(
            project.owner("example"),
            inventory=inventory,
        )
    ) == ("flow-catalog",)
    source_snapshot = next(
        snapshot
        for snapshot in inventory
        if snapshot.contract_kind == "source-assets"
    )
    with pytest.raises(TypeError):
        source_snapshot.document["payload"]["values"][0] = "changed"
    with pytest.raises(ValueError, match="identity drift"):
        project.owner_flow_catalog_snapshots(
            project.owner("example"),
            inventory=(
                replace(source_snapshot, document=dict(source_snapshot.document)),
            ),
        )
    mutable_document = MappingProxyType(
        {
            **source_snapshot.document,
            "payload": UserDict({"values": ("fixture",)}),
        }
    )
    with pytest.raises(ValueError, match="identity drift"):
        project.owner_flow_catalog_snapshots(
            project.owner("example"),
            inventory=(
                replace(source_snapshot, document=mutable_document),
            ),
        )


def test_check_designs_reads_each_platform_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_test_layout_platform(tmp_path, key="first")
    write_test_platform(tmp_path, key="second")
    catalog = (tmp_path / "configs/platform/catalog.toml").resolve()
    catalog.write_text(
        '''schema = 1
contract_kind = "platform-catalog"
path_scope = "repository"
owner = "test"

[platforms]
first = "first/platform.toml"
second = "second/platform.toml"
''',
        encoding="utf-8",
    )
    second_root = tmp_path / "configs/platform/second"
    for source in second_root.glob("*.toml"):
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                'owner = "test-platform"',
                'owner = "second-platform"',
            ),
            encoding="utf-8",
        )
    platform_sources = {
        catalog,
        *(
            path.resolve()
            for key in ("first", "second")
            for path in (tmp_path / "configs/platform" / key).glob("*.toml")
        ),
    }
    reads = {path: 0 for path in platform_sources}
    original_load = tomllib.load

    def counted_load(stream):
        path = Path(stream.name).resolve()
        if path in reads:
            reads[path] += 1
        return original_load(stream)

    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert set(reads.values()) == {1}


def test_repository_workflows_share_one_platform_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_test_platform(tmp_path)
    _write_release_target(tmp_path)
    component = tmp_path / "ip/fixture/component.toml"
    dependency = tmp_path / "ip/fixture/internal/component.toml"
    dependency.parent.mkdir(parents=True)
    dependency.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture"

name = "fixture-internal"
kind = "rtl-ip"

[filesets]
''',
        encoding="utf-8",
    )
    architecture = tmp_path / "ip/fixture/architecture.toml"
    architecture.write_text(
        '''schema = 1
contract_kind = "ip-architecture-behavior"
path_scope = "owner"
owner = "fixture"
''',
        encoding="utf-8",
    )
    component.write_text(
        component.read_text(encoding="utf-8")
        + 'architecture = ["ip/fixture/architecture.toml"]\n'
        + '''
[[component]]
name = "fixture-internal"
contract = "ip/fixture/internal/component.toml"
'''
        + "\n[variants.fixture]\ncontract = 'unused.toml'\n",
        encoding="utf-8",
    )
    planned_simulation_path = tmp_path / "ip/fixture/planned_simulation.toml"
    planned_simulation_path.write_text("schema = 3\n", encoding="utf-8")
    planned_simulation = SimpleNamespace(
        path=planned_simulation_path.resolve(),
        source_documents=MappingProxyType(
            {
                planned_simulation_path.resolve(): freeze_toml_document(
                    {"schema": 3}
                )
            }
        ),
    )
    planned_design_path = tmp_path / "ip/fixture/planned_design.toml"
    planned_design_path.write_text("schema = 1\n", encoding="utf-8")
    planned_design = SimpleNamespace(
        path=planned_design_path.resolve(),
        source_documents=MappingProxyType(
            {
                planned_design_path.resolve(): freeze_toml_document(
                    {"schema": 1}
                )
            }
        ),
    )
    planned_layout_path = tmp_path / "ip/fixture/planned_layout.toml"
    planned_layout_path.write_text("schema = 1\n", encoding="utf-8")
    planned_layout = SimpleNamespace(
        path=planned_layout_path.resolve(),
        source_documents=MappingProxyType(
            {
                planned_layout_path.resolve(): freeze_toml_document(
                    {"schema": 1}
                )
            }
        ),
    )
    platform_sources = {
        (tmp_path / "configs/platform/catalog.toml").resolve(),
        *(
            path.resolve()
            for path in (tmp_path / "configs/platform/testpdk").glob("*.toml")
        ),
    }
    reads = {path: 0 for path in platform_sources}
    original_load = tomllib.load
    observed_platform_inventories: list[object] = []
    observed_release_inventories: list[object] = []
    observed_oa_inventories: list[object] = []
    observed_oa_plan_inventories: list[object] = []
    observed_architecture_inventories: list[object] = []
    observed_source_ledgers: list[object] = []
    oa_source_reads: list[Path] = []
    oa_document_reads = 0
    architecture_reads = 0
    dependency_reads = 0

    def counted_load(stream):
        nonlocal architecture_reads, dependency_reads, oa_document_reads
        path = Path(stream.name).resolve()
        if path in reads:
            reads[path] += 1
        if path == (tmp_path / "ip/fixture/oa.toml").resolve():
            oa_document_reads += 1
        if path == architecture.resolve():
            architecture_reads += 1
        if path == dependency.resolve():
            dependency_reads += 1
        return original_load(stream)

    integration_contract = SimpleNamespace(
        name="fixture",
        source_documents=MappingProxyType({}),
    )

    def load_integration(_path, *, project, variant_source_documents):
        assert project.project_root == tmp_path.resolve()
        assert variant_source_documents is None
        return integration_contract

    def plan_integration(
        contract,
        *,
        platform_inventory,
        release_inventory,
        oa_source_inventory,
        oa_plan_inventory,
    ):
        assert contract is integration_contract
        observed_platform_inventories.append(platform_inventory)
        observed_release_inventories.append(release_inventory)
        observed_oa_inventories.append(oa_source_inventory)
        observed_oa_plan_inventories.append(oa_plan_inventory)
        return {"ip": "fixture"}

    def plan_oa(
        _path,
        *,
        project,
        platform_inventory,
        oa_source_inventory,
        architecture_source_documents,
    ):
        assert project.project_root == tmp_path.resolve()
        observed_platform_inventories.append(platform_inventory)
        observed_oa_inventories.append(oa_source_inventory)
        observed_architecture_inventories.append(
            architecture_source_documents
        )
        return SimpleNamespace(
            as_dict=lambda: {},
            designs=(
                SimpleNamespace(
                    inspection=SimpleNamespace(spec=planned_design),
                ),
            ),
            layouts=(SimpleNamespace(spec=planned_layout),),
            testbenches=(SimpleNamespace(simulation=planned_simulation),),
        )

    def load_oa_source(path, *, project):
        resolved = path.resolve()
        oa_source_reads.append(resolved)
        return SimpleNamespace(
            manifest_path=resolved,
            project=project,
            source_roots=(
                SimpleNamespace(
                    manifest_path=resolved,
                    owner="fixture",
                ),
            ),
            source_documents=MappingProxyType(
                {
                    resolved: freeze_toml_document(
                        {
                            "schema": 1,
                            "contract_kind": "oa-assembly",
                            "path_scope": "owner",
                            "owner": "fixture",
                            "name": "fixture",
                        }
                    )
                }
            ),
        )

    original_inspect_sources = (
        repository_checks.inspect_project_configuration_sources
    )

    def inspect_sources(*args, sources, **kwargs):
        observed_source_ledgers.append(sources)
        reads_before_scan = dependency_reads
        result = original_inspect_sources(*args, sources=sources, **kwargs)
        assert dependency_reads == reads_before_scan
        return result

    monkeypatch.setattr(tomllib, "load", counted_load)
    monkeypatch.setattr(
        repository_checks,
        "load_ip_integration_contract",
        load_integration,
    )
    monkeypatch.setattr(
        repository_checks,
        "plan_ip_integration_contract",
        plan_integration,
    )
    monkeypatch.setattr(repository_checks, "plan_oa_library_rebuild", plan_oa)
    monkeypatch.setattr(
        repository_checks,
        "resolve_layout_spec",
        lambda path, *, project, snapshot, platform: snapshot,
    )
    monkeypatch.setattr(
        repository_checks,
        "load_oa_library_source",
        load_oa_source,
    )
    monkeypatch.setattr(
        "sigilicon.domain.oa_library.resolve_oa_library_source",
        lambda _path, *, project, snapshot: snapshot,
    )
    monkeypatch.setattr(
        repository_checks,
        "inspect_project_configuration_sources",
        inspect_sources,
    )

    report = repository_checks.inspect_repository_designs(
        Project.from_project_root(tmp_path)
    )

    assert report["passed"] is True
    assert len(observed_platform_inventories) == 2
    assert all(
        inventory is observed_platform_inventories[0]
        for inventory in observed_platform_inventories[1:]
    )
    assert set(observed_platform_inventories[0]) == {"testpdk"}
    assert len(observed_release_inventories) == 1
    assert set(observed_release_inventories[0]) == {"fixture"}
    assert len(oa_source_reads) == 1
    assert len(observed_oa_inventories) == 2
    assert all(
        inventory is observed_oa_inventories[0]
        for inventory in observed_oa_inventories[1:]
    )
    assert set(observed_oa_inventories[0]) == set(oa_source_reads)
    assert len(observed_oa_plan_inventories) == 1
    assert set(observed_oa_plan_inventories[0]) == set(oa_source_reads)
    assert len(observed_architecture_inventories) == 1
    assert set(observed_architecture_inventories[0]) == {architecture.resolve()}
    assert len(observed_source_ledgers) == 1
    ledger = observed_source_ledgers[0]
    assert ledger.project.project_root == tmp_path.resolve()
    assert {
        planned_simulation_path.resolve(),
        planned_design_path.resolve(),
        planned_layout_path.resolve(),
        architecture.resolve(),
        dependency.resolve(),
        *platform_sources,
        *oa_source_reads,
    } <= set(ledger.documents)
    assert architecture_reads == 1
    assert dependency_reads >= 1
    assert oa_document_reads == 0
    assert set(reads.values()) == {1}
