"""Strict canonical serialization shared by immutable domain values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Any


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return _primitive(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, list):
        return [_primitive(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical object keys must be strings")
            result[key] = _primitive(item)
        return result
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("canonical numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot canonically serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _primitive(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def canonical_digest(value: Any) -> str:
    """Return the unambiguous SHA-256 identity of a canonical value."""

    encoded = canonical_json(value).encode("utf-8")
    return f"sha256-{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "canonical_digest",
    "canonical_json",
]
