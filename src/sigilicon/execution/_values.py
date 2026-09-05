"""Strict portable values and identities shared by the execution lifecycle."""

from __future__ import annotations
import hashlib
import math
import re
from types import MappingProxyType
from typing import Any, Mapping
from sigilicon.contracts import require_relative_path
from sigilicon.paths import validate_artifact_component


_ADAPTER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")


_DIGEST = re.compile(r"sha256-[0-9a-f]{64}\Z")


_RESOURCE = re.compile(r"[A-Za-z][A-Za-z0-9._:/-]{0,255}\Z")


_ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


_ROLES = frozenset({"diagnostic", "regression", "qualification", "signoff"})


_LEVELS = frozenset({"l0", "l1", "l2", "l3", "l4"})


_STEP_STATUSES = frozenset(
    {"succeeded", "failed", "blocked", "partial", "uncertain", "cancelled"}
)


_RUN_STATUSES = frozenset({"succeeded", "failed", "partial", "uncertain", "cancelled"})


_RUN_FAILURE_STATUSES = frozenset({"failed", "partial", "uncertain", "cancelled"})


class ContractError(ValueError):
    """An operation, plan, or adapter value violates the execution contract."""


class ExecutionError(RuntimeError):
    """A managed operation could not be executed or restored safely."""


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be an identifier")
    try:
        return validate_artifact_component(value, label)
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def adapter_identity(value: object) -> str:
    if not isinstance(value, str) or _ADAPTER.fullmatch(value) is None:
        raise ContractError(f"invalid adapter identity: {value!r}")
    return value


def resource_identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or _RESOURCE.fullmatch(value) is None
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ContractError(f"invalid external resource identity: {value!r}")
    return value


def resource_materialization_key(identity: str) -> str:
    logical = resource_identity(identity)
    return "resource-" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def _source_name(value: object) -> str:
    try:
        return require_relative_path(value, "source name").as_posix()
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def _freeze(value: Any, label: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError(f"{label} contains a non-string key")
        return MappingProxyType({key: _freeze(item, label) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, label) for item in value)
    raise ContractError(f"{label} contains non-portable {type(value).__name__}")


def json_value(value: Any) -> Any:
    """Return a portable mutable projection of a frozen execution value."""

    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


JsonScalar = None | bool | int | float | str


JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
