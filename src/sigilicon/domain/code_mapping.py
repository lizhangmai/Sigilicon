"""Configurable integer-score to integer-code mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.domain.config_contracts import (
    is_frozen_toml_document,
    require_config_header,
)


_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


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


def load_integer_code_mapping_contract(
    path: Path,
    *,
    contract_kind: str,
    table_path: tuple[str, ...],
    source_documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> IntegerCodeMapping:
    """Load a mapping from an explicitly described caller-owned contract."""

    contract_path = path.resolve()
    if source_documents is None:
        try:
            with contract_path.open("rb") as stream:
                raw = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"cannot read code mapping contract {contract_path}: {exc}"
            ) from exc
    else:
        if not isinstance(source_documents, _MAPPING_PROXY_TYPE):
            raise ValueError("code mapping source inventory must be immutable")
        try:
            raw = source_documents[contract_path]
        except KeyError as exc:
            raise ValueError(
                f"code mapping source inventory has no {contract_path} entry"
            ) from exc
        if (
            not contract_path.is_file()
            or not isinstance(raw, Mapping)
            or not is_frozen_toml_document(raw)
        ):
            raise ValueError("code mapping source snapshot identity drift")
    require_config_header(
        raw,
        contract_path,
        contract_kind=contract_kind,
        path_scope="owner",
    )
    value: object = raw
    for name in table_path:
        if not isinstance(value, Mapping):
            raise ValueError(
                f"code mapping contract lacks table {'.'.join(table_path)}"
            )
        value = value.get(name)
    return load_integer_code_mapping(value, ".".join(table_path))
