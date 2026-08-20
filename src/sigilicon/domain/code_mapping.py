"""Configurable integer-score to integer-code mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Mapping

from sigilicon.domain.config_contracts import require_config_header


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


@dataclass(frozen=True)
class IntegerCodeMapping:
    """One declarative affine mapping with integer endpoint saturation."""

    score_multiplier: int
    code_offset: int
    minimum_code: int
    maximum_code: int

    def code_for(self, score: int) -> int:
        if isinstance(score, bool) or not isinstance(score, int):
            raise TypeError("score must be an integer")
        affine = self.score_multiplier * score + self.code_offset
        return min(self.maximum_code, max(self.minimum_code, affine))

    @property
    def description(self) -> str:
        return (
            "code=clamp(score_multiplier*score+code_offset,"
            "minimum_code,maximum_code)"
        )

    def as_dict(self) -> dict[str, int | str]:
        return {
            "kind": "affine_saturating",
            "score_multiplier": self.score_multiplier,
            "code_offset": self.code_offset,
            "minimum_code": self.minimum_code,
            "maximum_code": self.maximum_code,
        }


def load_integer_code_mapping(value: object, field: str) -> IntegerCodeMapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    required = {
        "kind",
        "score_multiplier",
        "code_offset",
        "minimum_code",
        "maximum_code",
    }
    if set(value) != required:
        raise ValueError(f"{field} fields must be exactly {', '.join(sorted(required))}")
    if value.get("kind") != "affine_saturating":
        raise ValueError(f"{field}.kind must be affine_saturating")
    mapping = IntegerCodeMapping(
        score_multiplier=_integer(
            value.get("score_multiplier"), f"{field}.score_multiplier"
        ),
        code_offset=_integer(value.get("code_offset"), f"{field}.code_offset"),
        minimum_code=_integer(value.get("minimum_code"), f"{field}.minimum_code"),
        maximum_code=_integer(value.get("maximum_code"), f"{field}.maximum_code"),
    )
    if mapping.score_multiplier == 0:
        raise ValueError(f"{field}.score_multiplier must be non-zero")
    if mapping.minimum_code < 0 or mapping.minimum_code >= mapping.maximum_code:
        raise ValueError(f"{field} code limits must be ordered and non-negative")
    return mapping


def load_ip_adc_code_mapping(path: Path) -> IntegerCodeMapping:
    """Load the IP-owned ADC mapping from its behavioral contract."""

    contract_path = path.resolve()
    ip_indices = [
        index for index, part in enumerate(contract_path.parts) if part == "ip"
    ]
    if not ip_indices or ip_indices[-1] + 1 >= len(contract_path.parts):
        raise ValueError("ADC mapping contract must be below ip/<owner>")
    owner_directory = contract_path.parts[ip_indices[-1] + 1]
    if owner_directory == "legacy":
        raise ValueError("ADC mapping contract must belong to an active IP")
    expected_owner = owner_directory.replace("_", "-")
    try:
        with contract_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read ADC mapping contract {contract_path}: {exc}") from exc
    require_config_header(
        raw,
        contract_path,
        contract_kind="ip-architecture-behavior",
        path_scope="owner",
        owner=expected_owner,
    )
    adc = raw.get("adc")
    if not isinstance(adc, Mapping):
        raise ValueError("ADC mapping contract must contain an adc table")
    return load_integer_code_mapping(adc.get("code_mapping"), "adc.code_mapping")
