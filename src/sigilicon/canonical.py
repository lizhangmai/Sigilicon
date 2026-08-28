"""Strict canonical serialization shared by immutable domain values."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import json
from types import UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints


_T = TypeVar("_T")


class CanonicalSerializationError(ValueError):
    """A value is not a strict canonical structure."""


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _primitive(getattr(value, field.name))
            for field in fields(value)
        }
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


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalSerializationError(
                f"canonical JSON contains duplicate field {key!r}"
            )
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise CanonicalSerializationError(
        f"canonical JSON contains non-finite number {value!r}"
    )


def _decode(value: Any, expected: Any, path: str) -> Any:
    if expected is Any:
        return value
    if expected is type(None):
        if value is not None:
            raise CanonicalSerializationError(f"{path} must be null")
        return None
    if expected is bool:
        if type(value) is not bool:
            raise CanonicalSerializationError(f"{path} must be a boolean")
        return value
    if expected is int:
        if type(value) is not int:
            raise CanonicalSerializationError(f"{path} must be an integer")
        return value
    if expected is float:
        if type(value) is not float:
            raise CanonicalSerializationError(f"{path} must be a float")
        return value
    if expected is str:
        if type(value) is not str:
            raise CanonicalSerializationError(f"{path} must be a string")
        return value
    if isinstance(expected, type) and issubclass(expected, Enum):
        if type(value) is not str:
            raise CanonicalSerializationError(
                f"{path} must be a {expected.__name__} string"
            )
        try:
            return expected(value)
        except ValueError as exc:
            raise CanonicalSerializationError(
                f"{path} contains unknown {expected.__name__} value {value!r}"
            ) from exc

    origin = get_origin(expected)
    arguments = get_args(expected)
    if origin in {UnionType, Union}:
        failures: list[str] = []
        for option in arguments:
            try:
                return _decode(value, option, path)
            except CanonicalSerializationError as exc:
                failures.append(str(exc))
        raise CanonicalSerializationError(
            f"{path} does not match any allowed structure: {'; '.join(failures)}"
        )
    if origin is tuple:
        if not isinstance(value, list):
            raise CanonicalSerializationError(f"{path} must be an array")
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(
                _decode(item, arguments[0], f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        if len(value) != len(arguments):
            raise CanonicalSerializationError(
                f"{path} must contain exactly {len(arguments)} items"
            )
        return tuple(
            _decode(item, item_type, f"{path}[{index}]")
            for index, (item, item_type) in enumerate(zip(value, arguments))
        )

    if isinstance(expected, type) and is_dataclass(expected):
        if not isinstance(value, dict):
            raise CanonicalSerializationError(
                f"{path} must be a {expected.__name__} object"
            )
        expected_fields = {item.name: item for item in fields(expected)}
        missing = set(expected_fields) - set(value)
        unknown = set(value) - set(expected_fields)
        if missing or unknown:
            raise CanonicalSerializationError(
                f"{path} fields do not match {expected.__name__}: "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        annotations = get_type_hints(expected)
        decoded = {
            name: _decode(value[name], annotations[name], f"{path}.{name}")
            for name in expected_fields
        }
        try:
            return expected(**decoded)
        except (TypeError, ValueError) as exc:
            raise CanonicalSerializationError(
                f"{path} is not a valid {expected.__name__}: {exc}"
            ) from exc

    raise TypeError(f"unsupported canonical target type: {expected!r}")


def canonical_from_json(text: str, expected: type[_T]) -> _T:
    """Decode one exact typed value, rejecting drift and ambiguous JSON."""

    if not isinstance(text, str):
        raise CanonicalSerializationError("canonical JSON input must be text")
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_constant,
        )
    except CanonicalSerializationError:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalSerializationError(f"invalid canonical JSON: {exc}") from exc
    return _decode(raw, expected, expected.__name__)


def canonical_from_exact_json(text: str, expected: type[_T]) -> _T:
    """Decode one typed value and require its exact canonical representation."""

    value = canonical_from_json(text, expected)
    if canonical_json(value) != text:
        raise CanonicalSerializationError(
            f"{expected.__name__} JSON is valid but not canonical"
        )
    return value


__all__ = [
    "CanonicalSerializationError",
    "canonical_from_exact_json",
    "canonical_from_json",
    "canonical_json",
]
