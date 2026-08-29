from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from conftest import write_component_owner
import sigilicon.domain.repository as repository_module
import sigilicon.workflows.repository_checks as repository_checks_module
from sigilicon.cli.check_designs import main as check_designs_main


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


def test_check_designs_reuses_the_loaded_component_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = write_component_owner(tmp_path, "example", filesets={}).resolve()
    original = repository_checks_module.read_toml
    component_reads = 0

    def counted(path: Path):
        nonlocal component_reads
        if path.resolve() == component:
            component_reads += 1
        return original(path)

    monkeypatch.setattr(repository_checks_module, "read_toml", counted)
    monkeypatch.chdir(tmp_path)

    assert check_designs_main([]) == 0
    assert component_reads == 1


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
