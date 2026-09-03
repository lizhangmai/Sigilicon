from dataclasses import replace
from pathlib import Path

import pytest

import sigilicon.project._component as component_domain
from sigilicon.project._component import (
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
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[sources]
library = "ip/shared/library.py"

[filesets]
python = ["library"]
''',
        encoding="utf-8",
    )

    loaded = load_component_contract(contract, project_root=tmp_path)

    assert loaded.kind == "source-library"
    assert loaded.lifecycle == "active"
    assert loaded.sources["library"].as_posix() == "ip/shared/library.py"
    assert loaded.filesets["python"] == (loaded.sources["library"],)
    with pytest.raises(TypeError):
        loaded.filesets["python"] = ()
    with pytest.raises(TypeError):
        loaded.document["kind"] = "rtl-ip"
    assert loaded.document["filesets"]["python"] == (
        "library",
    )
    with pytest.raises(TypeError):
        loaded.document["filesets"]["python"][0] = "changed.py"


def test_component_lifecycle_is_typed_and_frozen(tmp_path: Path) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"
lifecycle = "legacy"

[sources]
library = "ip/shared/library.py"

[filesets]
python = ["library"]
''',
        encoding="utf-8",
    )

    loaded = load_component_contract(contract, project_root=tmp_path)

    assert loaded.lifecycle == "legacy"
    with pytest.raises(TypeError):
        loaded.document["lifecycle"] = "active"

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'lifecycle = "legacy"', 'lifecycle = "retired"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported component lifecycle"):
        load_component_contract(contract, project_root=tmp_path)


def test_component_filesets_only_compose_unique_source_identities(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[sources]
library = "ip/shared/library.py"

[filesets]
python = ["missing"]
''',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown sources"):
        load_component_contract(contract, project_root=tmp_path)

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'library = "ip/shared/library.py"',
            'library = "ip/shared/library.py"\nalias = "ip/shared/library.py"',
        ).replace('python = ["missing"]', 'python = ["library"]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="multiple identities"):
        load_component_contract(contract, project_root=tmp_path)


def test_component_roles_reference_source_identities(tmp_path: Path) -> None:
    owner = tmp_path / "ip/shared"
    owner.mkdir(parents=True)
    operations = owner / "operations.toml"
    operations.write_text("operations\n", encoding="utf-8")
    contract = owner / "ip.toml"
    contract.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "rtl-ip"
operation_catalog = "operations"

[sources]
operations = "ip/shared/operations.toml"
''',
        encoding="utf-8",
    )

    loaded = load_component_contract(contract, project_root=tmp_path)
    assert loaded.operation_catalog == loaded.sources["operations"]

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'operation_catalog = "operations"',
            'operation_catalog = "ip/shared/operations.toml"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="references unknown source"):
        load_component_contract(contract, project_root=tmp_path)


def test_component_graph_rejects_a_snapshot_from_another_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[sources]
library = "ip/shared/library.py"

[filesets]
python = ["library"]
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
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "child"
name = "child"
kind = "source-library"

[sources]
library = "ip/child/source.py"

[filesets]
python = ["library"]
''',
        encoding="utf-8",
    )
    root_contract_path = tmp_path / "ip/top/ip.toml"
    root_contract_path.parent.mkdir(parents=True)
    root_contract_path.write_text(
        '''schema = 3
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
