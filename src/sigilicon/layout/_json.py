"""Strict JSON primitives shared by the layout process boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def record_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object")
    return value


def record(
    value: object,
    label: str,
    fields: set[str],
    *,
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    raw = record_mapping(value, label)
    unknown = set(raw) - fields
    missing = fields - optional - set(raw)
    if unknown or missing:
        raise ValueError(
            f"{label} fields disagree: missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    return raw


def array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty):
        raise ValueError(f"{label} must be text")
    return value


def strings(value: object, label: str) -> tuple[str, ...]:
    result = tuple(text(item, f"{label}[]") for item in array(value, label))
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return result


def optional_text(value: object, label: str) -> str | None:
    return None if value is None else text(value, label)


def positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def int_pair(value: object, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ValueError(f"{label} must contain two integers")
    return value[0], value[1]


def string_tuple(value: object, label: str, size: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{label} must contain {size} strings")
    return tuple(text(item, label) for item in value)
