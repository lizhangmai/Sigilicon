from __future__ import annotations

from pathlib import Path
import tomllib
from types import MappingProxyType

import pytest

import sigilicon.domain.code_mapping as code_mapping_domain
from sigilicon.domain.code_mapping import (
    load_integer_code_mapping,
    load_integer_code_mapping_contract,
)
from sigilicon.domain.config_contracts import freeze_toml_document


def test_affine_saturating_mapping_clamps_both_ends() -> None:
    mapping = load_integer_code_mapping(
        {
            "kind": "affine_saturating",
            "score_multiplier": 1,
            "code_offset": 3,
            "minimum_code": 1,
            "maximum_code": 6,
        },
        "mapping",
    )

    assert [mapping.code_for(score) for score in (-4, -2, 0, 3, 8)] == [
        1,
        1,
        3,
        6,
        6,
    ]


def _write_behavior_contract(path: Path, *, owner: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        f'''schema = 1
contract_kind = "ip-architecture-behavior"
path_scope = "owner"
owner = "{owner}"

[adc.code_mapping]
kind = "affine_saturating"
score_multiplier = 1
code_offset = 32
minimum_code = 0
maximum_code = 63
''',
        encoding="utf-8",
    )


def test_mapping_contract_uses_explicit_kind_and_table_path(
    tmp_path: Path,
) -> None:
    contract = (
        tmp_path
        / "ip/example/configs/architecture/behavioral_contract.toml"
    )
    _write_behavior_contract(contract, owner="example")

    mapping = load_integer_code_mapping_contract(
        contract,
        contract_kind="ip-architecture-behavior",
        table_path=("adc", "code_mapping"),
    )

    assert mapping.code_for(-33) == 0
    assert mapping.code_for(0) == 32
    assert mapping.code_for(32) == 63


def test_mapping_contract_reuses_an_immutable_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = tmp_path / "ip/example/configs/behavior.toml"
    _write_behavior_contract(contract, owner="fixture-owner")
    resolved = contract.resolve()
    with contract.open("rb") as stream:
        document = freeze_toml_document(tomllib.load(stream))
    inventory = MappingProxyType({resolved: document})
    monkeypatch.setattr(
        code_mapping_domain.tomllib,
        "load",
        lambda *_args, **_kwargs: pytest.fail("mapping contract was reloaded"),
    )

    mapping = load_integer_code_mapping_contract(
        contract,
        contract_kind="ip-architecture-behavior",
        table_path=("adc", "code_mapping"),
        source_documents=inventory,
    )

    assert mapping.code_for(0) == 32
    with pytest.raises(ValueError, match="inventory has no"):
        load_integer_code_mapping_contract(
            contract,
            contract_kind="ip-architecture-behavior",
            table_path=("adc", "code_mapping"),
            source_documents=MappingProxyType({}),
        )
    with pytest.raises(ValueError, match="immutable"):
        load_integer_code_mapping_contract(
            contract,
            contract_kind="ip-architecture-behavior",
            table_path=("adc", "code_mapping"),
            source_documents={resolved: document},
        )
