from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.domain.code_mapping import (
    load_integer_code_mapping,
    load_ip_adc_code_mapping,
)


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


def test_affine_saturating_mapping_is_configurable_without_caller_changes() -> None:
    mapping = load_integer_code_mapping(
        {
            "kind": "affine_saturating",
            "score_multiplier": 2,
            "code_offset": 7,
            "minimum_code": 1,
            "maximum_code": 14,
        },
        "mapping",
    )

    assert [mapping.code_for(score) for score in (-8, -1, 0, 2, 8)] == [
        1,
        5,
        7,
        11,
        14,
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


def test_ip_adc_mapping_derives_the_active_owner_from_its_path(
    tmp_path: Path,
) -> None:
    contract = (
        tmp_path
        / "ip/cim_compute_nocal/configs/architecture/behavioral_contract.toml"
    )
    _write_behavior_contract(contract, owner="cim-compute-nocal")

    mapping = load_ip_adc_code_mapping(contract)

    assert mapping.code_for(-33) == 0
    assert mapping.code_for(0) == 32
    assert mapping.code_for(32) == 63


def test_ip_adc_mapping_rejects_an_owner_that_disagrees_with_its_path(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "ip/cim_compute_nocal/configs/behavior.toml"
    _write_behavior_contract(contract, owner="cim-compute")

    with pytest.raises(ValueError, match="owner must be 'cim-compute-nocal'"):
        load_ip_adc_code_mapping(contract)
