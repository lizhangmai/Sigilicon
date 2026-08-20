from pathlib import Path

import pytest

from sigilicon.domain.config_contracts import (
    require_config_header,
    validate_configuration_inventory,
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_inventory_validates_active_native_and_excluded_boundaries(tmp_path: Path) -> None:
    header = """schema = 1
contract_kind = "test-contract"
path_scope = "owner"
owner = "test"
"""
    _write(tmp_path, "active/root.toml", header)
    _write(tmp_path, "active/cell/CELL/cell.toml", header.replace("test-contract", "oa-cell"))
    _write(tmp_path, "native/tb/simulation.toml", "schema = 3\n")
    _write(tmp_path, "excluded/catalog.toml", "schema = 1\n")
    _write(tmp_path, "excluded/source.toml", "fixture = true\n")
    _write(tmp_path, "pixi.toml", "[workspace]\nname = \"fixture\"\n")
    inventory = """schema = 1
contract_kind = "configuration-inventory"
path_scope = "repository"
owner = "repository"

[[files]]
path = "active/root.toml"
contract_kind = "test-contract"
path_scope = "owner"
owner = "test"

[[families]]
glob = "active/**/cell.toml"
contract_kind = "oa-cell"
path_scope = "owner"
owner = "test"

[[native_families]]
glob = "native/**/simulation.toml"
schema = 3

[[excluded_files]]
path = "pixi.toml"
reason = "tool-owned manifest"

[[excluded_families]]
glob = "excluded/**/*.toml"
exclude = ["excluded/catalog.toml"]
reason = "source files excluded by repository policy"
"""
    inventory_path = tmp_path / "inventory.toml"
    inventory_path.write_text(inventory, encoding="utf-8")

    report = validate_configuration_inventory(inventory_path, project_root=tmp_path)

    assert report["passed"] is True
    assert report["files"] == 1
    assert report["families"]["families[0]"] == 1
    assert report["native_families"]["native_families[0]"] == 1
    assert report["excluded_files"] == 1
    assert report["excluded_families"]["excluded_families[0]"] == 1


def test_common_header_rejects_wrong_scope() -> None:
    with pytest.raises(ValueError, match="path_scope"):
        require_config_header(
            {
                "schema": 1,
                "contract_kind": "test-contract",
                "path_scope": "cell",
                "owner": "test",
            },
            Path("fixture.toml"),
            contract_kind="test-contract",
            path_scope="owner",
            owner="test",
        )
