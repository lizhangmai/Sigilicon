from pathlib import Path

import pytest

from sigilicon.domain.component import resolve_component_contract
from sigilicon.project import Project


def _catalog_component(root: Path, owner: str, contract: Path) -> None:
    catalog = root / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + f'''\n[components.{owner}]
contract = "{contract.relative_to(root).as_posix()}"
root = "{contract.parent.relative_to(root).as_posix()}"
''',
        encoding="utf-8",
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

    _catalog_component(tmp_path, "shared", contract)
    loaded = Project.open(tmp_path).owner("shared").component

    assert loaded.kind == "source-library"
    assert loaded.lifecycle == "active"
    assert loaded.sources["library"].as_posix() == "ip/shared/library.py"
    assert loaded.filesets["python"] == (loaded.sources["library"],)
    with pytest.raises(TypeError):
        loaded.filesets["python"] = ()


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

    _catalog_component(tmp_path, "shared", contract)
    loaded = Project.open(tmp_path).owner("shared").component

    assert loaded.lifecycle == "legacy"

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'lifecycle = "legacy"', 'lifecycle = "retired"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported component lifecycle"):
        Project.open(tmp_path)


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

    _catalog_component(tmp_path, "shared", contract)
    with pytest.raises(ValueError, match="unknown sources"):
        Project.open(tmp_path)

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'library = "ip/shared/library.py"',
            'library = "ip/shared/library.py"\nalias = "ip/shared/library.py"',
        ).replace('python = ["missing"]', 'python = ["library"]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="multiple identities"):
        Project.open(tmp_path)


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

    _catalog_component(tmp_path, "shared", contract)
    loaded = Project.open(tmp_path).owner("shared").component
    assert loaded.operation_catalog == loaded.sources["operations"]

    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'operation_catalog = "operations"',
            'operation_catalog = "ip/shared/operations.toml"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="references unknown source"):
        Project.open(tmp_path)


def test_component_snapshot_rejects_current_document_drift(tmp_path: Path) -> None:
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
    _catalog_component(tmp_path, "shared", contract)
    project = Project.open(tmp_path)
    snapshot = project.owner("shared").component
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'kind = "source-library"',
            'kind = "rtl-ip"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="component snapshot source document drift"):
        resolve_component_contract(contract, project=project, snapshot=snapshot)


def test_owner_identity_captures_uncataloged_transitive_component(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ip/leaf/leaf.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module leaf; endmodule\n", encoding="utf-8")
    leaf = tmp_path / "ip/leaf/component.toml"
    leaf.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "leaf"
name = "leaf"
kind = "rtl-ip"

[sources]
rtl = "ip/leaf/leaf.sv"

[filesets]
rtl = ["rtl"]
''',
        encoding="utf-8",
    )
    top_source = tmp_path / "ip/top/top.sv"
    top_source.parent.mkdir(parents=True)
    top_source.write_text("module top; leaf child(); endmodule\n", encoding="utf-8")
    top = tmp_path / "ip/top/component.toml"
    top.write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "top"
name = "top"
kind = "composite-ip"

[sources]
rtl = "ip/top/top.sv"

[filesets]
rtl = ["rtl"]

[[component]]
name = "leaf"
contract = "ip/leaf/component.toml"
''',
        encoding="utf-8",
    )
    _catalog_component(tmp_path, "top", top)

    project = Project.open(tmp_path)
    identity = project.operation_identity("top")
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace(
            'kind = "rtl-ip"',
            'kind = "source-library"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="component snapshot source document drift"):
        project.operation_identity("top")
    assert Project.open(tmp_path).operation_identity("top") != identity
