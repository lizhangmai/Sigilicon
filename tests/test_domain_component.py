from dataclasses import replace
from pathlib import Path

import pytest

import sigilicon.domain.component as component_domain
from sigilicon.domain.component import (
    ComponentContract,
    load_component_contract,
    load_component_graph,
    resolve_component_contract,
)


def test_source_library_is_a_first_class_component_kind(tmp_path: Path) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[filesets]
python = ["ip/shared/library.py"]
''',
        encoding="utf-8",
    )

    loaded = load_component_contract(contract, project_root=tmp_path)

    assert loaded.kind == "source-library"
    assert loaded.filesets["python"][0].as_posix() == "ip/shared/library.py"
    with pytest.raises(TypeError):
        loaded.filesets["python"] = ()
    with pytest.raises(TypeError):
        loaded.document["kind"] = "rtl-ip"
    assert loaded.document["filesets"]["python"] == (
        "ip/shared/library.py",
    )
    with pytest.raises(TypeError):
        loaded.document["filesets"]["python"][0] = "changed.py"


def test_component_contract_preserves_direct_construction_compatibility(
    tmp_path: Path,
) -> None:
    contract = ComponentContract(
        path=tmp_path / "ip/example/ip.toml",
        project_root=tmp_path,
        owner="example",
        name="example",
        kind="rtl-ip",
        public_interface=None,
        filesets={},
        components=(),
    )

    assert contract.document == {}


def test_component_graph_reuses_an_explicit_root_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[filesets]
python = ["ip/shared/library.py"]
''',
        encoding="utf-8",
    )
    root_contract = load_component_contract(contract, project_root=tmp_path)
    reads: list[Path] = []
    original_loader = component_domain.load_component_contract

    def tracked_loader(path: Path, *, project_root: Path):
        reads.append(path.resolve())
        return original_loader(path, project_root=project_root)

    monkeypatch.setattr(component_domain, "load_component_contract", tracked_loader)

    graph = load_component_graph(
        contract,
        project_root=tmp_path,
        root_contract=root_contract,
    )

    assert graph == {"shared": root_contract}
    assert reads == []

    legacy_root = replace(root_contract, document={})
    graph = load_component_graph(
        contract,
        project_root=tmp_path,
        root_contract=legacy_root,
    )

    assert graph["shared"] == root_contract
    assert reads == [contract.resolve()]


def test_component_graph_rejects_a_snapshot_from_another_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[filesets]
python = ["ip/shared/library.py"]
''',
        encoding="utf-8",
    )
    root_contract = load_component_contract(contract, project_root=tmp_path)

    with pytest.raises(ValueError, match="root snapshot disagrees"):
        load_component_graph(
            contract,
            project_root=tmp_path / "other",
            root_contract=root_contract,
        )


def test_component_graph_still_loads_dependencies_below_a_root_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child_source = tmp_path / "ip/child/source.py"
    child_source.parent.mkdir(parents=True)
    child_source.write_text("VALUE = 1\n", encoding="utf-8")
    child = tmp_path / "ip/child/ip.toml"
    child.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "child"
name = "child"
kind = "source-library"

[filesets]
python = ["ip/child/source.py"]
''',
        encoding="utf-8",
    )
    root_contract_path = tmp_path / "ip/top/ip.toml"
    root_contract_path.parent.mkdir(parents=True)
    root_contract_path.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "top"
name = "top"
kind = "composite-ip"

[[component]]
name = "child"
contract = "ip/child/ip.toml"
''',
        encoding="utf-8",
    )
    root_contract = load_component_contract(
        root_contract_path,
        project_root=tmp_path,
    )
    child_contract = load_component_contract(child, project_root=tmp_path)
    reads: list[Path] = []
    original_loader = component_domain.load_component_contract

    def tracked_loader(path: Path, *, project_root: Path):
        reads.append(path.resolve())
        return original_loader(path, project_root=project_root)

    monkeypatch.setattr(component_domain, "load_component_contract", tracked_loader)

    graph = load_component_graph(
        root_contract_path,
        project_root=tmp_path,
        root_contract=root_contract,
    )

    assert sorted(graph) == ["child", "top"]
    assert reads == [child.resolve()]

    reads.clear()
    graph = load_component_graph(
        root_contract_path,
        project_root=tmp_path,
        root_contract=root_contract,
        contract_inventory={
            root_contract.path: root_contract,
            child_contract.path: child_contract,
        },
    )

    assert graph == {"top": root_contract, "child": child_contract}
    assert reads == []

    with pytest.raises(ValueError, match="source document drift"):
        resolve_component_contract(
            root_contract_path,
            project_root=tmp_path,
            snapshot=replace(root_contract, kind="rtl-ip"),
        )

    with pytest.raises(ValueError, match="snapshot identity drift"):
        resolve_component_contract(
            root_contract_path,
            project_root=tmp_path,
            snapshot=replace(root_contract, filesets=dict(root_contract.filesets)),
        )

    with pytest.raises(ValueError, match="snapshot identity drift"):
        resolve_component_contract(
            root_contract_path,
            project_root=tmp_path,
            snapshot=replace(root_contract, document="forged"),
        )
