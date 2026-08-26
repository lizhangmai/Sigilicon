"""Canonical serialization for physical-design values."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
from typing import Any


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, list):
        return [_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot canonically serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _primitive(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
