from __future__ import annotations

from pathlib import Path
import tomllib
from types import SimpleNamespace

import pytest

from conftest import (
    write_component_owner,
    write_test_layout_platform,
    write_test_platform,
)
import sigilicon.domain.repository as repository_module
from sigilicon.cli.check_designs import main as check_designs_main
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
    (owner / "oa.toml").write_text("name = 'fixture'\n", encoding="utf-8")
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
    manifest_reads = 0

    def counted(path: Path):
        nonlocal manifest_reads
        if path.resolve() == contract:
            manifest_reads += 1
        return original(path)

    monkeypatch.setattr(repository_module, "read_toml", counted)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert manifest_reads == 1
    assert '"passed": true' in capsys.readouterr().out


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
        lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: {}),
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
    write_component_owner(
        tmp_path,
        "example",
        filesets={
            "flow": tuple(path.relative_to(tmp_path).as_posix() for path in catalogs)
        },
    )
    catalog_paths = {path.resolve() for path in catalogs}
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
