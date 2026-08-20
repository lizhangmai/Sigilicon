from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.domain.ip_release import load_ip_contract
from sigilicon.workflows.ip_packaging import release_role_view


def _contract_fixture(root: Path) -> Path:
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
default_qualification = "development"

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
[exports.qualification.development]
required_roles = ["interface_contract"]
[exports.qualification.implementation]
required_roles = ["interface_contract"]
[exports.qualification.signoff]
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
[exports.qualification.development]
required_roles = ["interface_contract"]
[exports.qualification.implementation]
required_roles = ["interface_contract"]
[exports.qualification.signoff]
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
    return contract


def test_one_ip_contract_exposes_multiple_scoped_circuits(tmp_path: Path) -> None:
    contract = load_ip_contract(_contract_fixture(tmp_path), project_root=tmp_path)

    assert contract.name == "fixture-ip"
    assert [item.name for item in contract.exports] == ["left", "right"]
    assert contract.get_export("left").oa_cell == "LEFT"
    assert contract.get_export("right").oa_cell == "RIGHT"
    assert [item.role for item in contract.collateral] == [
        "interface_contract",
        "interface_contract",
    ]


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
default_qualification = "development"
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
