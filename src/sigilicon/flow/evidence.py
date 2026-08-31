"""Strict, low-level facts exchanged across the Flow execution seam.

Domain evidence belongs to the owning domain (for example
``sigilicon.domain.physical_verification``).  This module deliberately does
not try to model that evidence.  It provides the small typed value contract
used by the generic Flow layer when an Adapter projects a domain observation
into policy facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from sigilicon.flow.errors import FactContractError, FactValueError


_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")


class FactKind(str, Enum):
    """The intentionally small set of values portable in a FactSet."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    REAL = "real"
    TEXT = "text"
    TEXT_LIST = "text-list"


FactAtom = bool | int | float | str | tuple[str, ...]


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise FactContractError(f"invalid {label}: {value!r}")
    return value


def _semantic_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise FactContractError(f"{label} must be a non-empty semantic identity")
    if Path(value).is_absolute() or re.match(r"[A-Za-z]:[\\/]", value):
        raise FactContractError(f"{label} cannot be an absolute site path")
    return value


@dataclass(frozen=True)
class FactSource:
    """The action and optional artifact that produced one FactSet."""

    action_kind: str
    node_id: str | None = None
    artifact_identity: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.action_kind, "fact source action kind")
        if self.node_id is not None:
            _identifier(self.node_id, "fact source node")
        if self.artifact_identity is not None:
            _semantic_identity(
                self.artifact_identity,
                "fact source artifact identity",
            )

    def to_json(self) -> dict[str, str | None]:
        return {
            "action_kind": self.action_kind,
            "node_id": self.node_id,
            "artifact_identity": self.artifact_identity,
        }

    @classmethod
    def from_json(cls, payload: object) -> "FactSource":
        if not isinstance(payload, dict) or set(payload) != {
            "action_kind",
            "node_id",
            "artifact_identity",
        }:
            raise FactValueError("Fact source JSON fields drift")
        try:
            return cls(
                action_kind=payload["action_kind"],
                node_id=payload["node_id"],
                artifact_identity=payload["artifact_identity"],
            )
        except FactContractError as exc:
            raise FactValueError(str(exc)) from exc


@dataclass(frozen=True)
class FactSpec:
    """One typed fact declaration in an Action's schema."""

    name: str
    kind: FactKind
    required: bool = True
    unit: str | None = None
    enum_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.name, "fact name")
        if not isinstance(self.kind, FactKind):
            raise FactContractError("fact kind must be a FactKind")
        if type(self.required) is not bool:
            raise FactContractError("fact required flag must be boolean")
        if self.unit is not None:
            _semantic_identity(self.unit, f"fact {self.name!r} unit")
            if self.kind not in {FactKind.INTEGER, FactKind.REAL}:
                raise FactContractError(
                    f"fact {self.name!r} unit is only valid for numeric facts"
                )
        values = tuple(self.enum_values)
        if any(not isinstance(value, str) or not value for value in values):
            raise FactContractError(
                f"fact {self.name!r} enum values must be non-empty strings"
            )
        if len(values) != len(set(values)):
            raise FactContractError(f"fact {self.name!r} enum values repeat")
        if values and self.kind is not FactKind.TEXT:
            raise FactContractError(
                f"fact {self.name!r} enum values require a text fact"
            )
        object.__setattr__(self, "enum_values", values)

    def descriptor(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "required": self.required,
            "unit": self.unit,
            "enum_values": list(self.enum_values),
        }

    def validate(
        self,
        value: object,
        label: str | None = None,
        *,
        contract: bool = False,
    ) -> FactAtom:
        prefix = self.name if label is None else label
        error = FactContractError if contract else FactValueError
        if self.kind is FactKind.BOOLEAN:
            if type(value) is not bool:
                raise error(f"{prefix} must be boolean")
            result: FactAtom = value
        elif self.kind is FactKind.INTEGER:
            # bool is an int subclass; exact type is intentional here.
            if type(value) is not int:
                raise error(f"{prefix} must be integer")
            result = value
        elif self.kind is FactKind.REAL:
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise error(f"{prefix} must be a finite real")
            result = value
        elif self.kind is FactKind.TEXT:
            if type(value) is not str:
                raise error(f"{prefix} must be text")
            if self.enum_values and value not in self.enum_values:
                raise error(
                    f"{prefix} must be one of {list(self.enum_values)!r}"
                )
            result = value
        else:
            if isinstance(value, list):
                value = tuple(value)
            if (
                not isinstance(value, tuple)
                or any(type(item) is not str for item in value)
            ):
                raise error(
                    f"{prefix} must be a text list"
                )
            result = tuple(value)
        return result


@dataclass(frozen=True)
class FactSchema:
    """The complete fact interface of one Action."""

    action_kind: str
    fields: tuple[FactSpec, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.action_kind, "fact schema action kind")
        fields = tuple(self.fields)
        if any(not isinstance(field, FactSpec) for field in fields):
            raise FactContractError("fact schema fields must be FactSpec values")
        names = tuple(field.name for field in fields)
        if len(names) != len(set(names)):
            raise FactContractError("fact schema fields repeat")
        object.__setattr__(self, "fields", fields)

    @property
    def required(self) -> tuple[FactSpec, ...]:
        return tuple(field for field in self.fields if field.required)

    @property
    def optional(self) -> tuple[FactSpec, ...]:
        return tuple(field for field in self.fields if not field.required)

    def field(self, name: str) -> FactSpec:
        for field in self.fields:
            if field.name == name:
                return field
        raise FactContractError(
            f"fact {name!r} is not declared by schema {self.action_kind!r}"
        )

    def descriptor(self) -> dict[str, object]:
        return {
            "action_kind": self.action_kind,
            "fields": [field.descriptor() for field in self.fields],
        }

    def project(
        self,
        values: Mapping[str, object],
        *,
        source: FactSource,
    ) -> "FactSet":
        return FactSet(self, values, source)


def _json_atom(value: FactAtom) -> object:
    if isinstance(value, tuple):
        return list(value)
    return value


@dataclass(frozen=True)
class FactSet:
    """Immutable typed facts validated against one Action schema."""

    schema: FactSchema
    values: Mapping[str, FactAtom]
    source: FactSource

    def __post_init__(self) -> None:
        if not isinstance(self.schema, FactSchema):
            raise FactValueError("FactSet schema must be a FactSchema")
        if not isinstance(self.source, FactSource):
            raise FactValueError("FactSet source must be a FactSource")
        if self.source.action_kind != self.schema.action_kind:
            raise FactValueError(
                "FactSet source action does not match its fact schema"
            )
        if not isinstance(self.values, Mapping):
            raise FactValueError("FactSet values must be a mapping")
        values = dict(self.values)
        if any(not isinstance(name, str) for name in values):
            raise FactValueError("FactSet names must be strings")
        declared = {field.name: field for field in self.schema.fields}
        unknown = set(values) - set(declared)
        if unknown:
            raise FactValueError(
                f"FactSet contains undeclared facts: {sorted(unknown)}"
            )
        missing = {
            field.name
            for field in self.schema.required
            if field.name not in values
        }
        if missing:
            raise FactValueError(
                f"FactSet omits required facts: {sorted(missing)}"
            )
        validated = {
            name: declared[name].validate(value, f"fact {name!r}")
            for name, value in values.items()
        }
        object.__setattr__(self, "values", MappingProxyType(validated))

    @classmethod
    def empty(
        cls,
        schema: FactSchema,
        *,
        source: FactSource,
    ) -> "FactSet":
        if schema.required:
            raise FactValueError(
                "cannot construct an empty FactSet for a schema with required facts"
            )
        return cls(schema, {}, source)

    def __getitem__(self, name: str) -> FactAtom:
        try:
            return self.values[name]
        except KeyError as exc:
            raise KeyError(name) from exc

    def __contains__(self, name: object) -> bool:
        return name in self.values

    def get(self, name: str, default: FactAtom | None = None) -> FactAtom | None:
        return self.values.get(name, default)

    def keys(self):
        return self.values.keys()

    def items(self):
        return self.values.items()

    def as_mapping(self) -> Mapping[str, FactAtom]:
        """Return a read-only value view for policy/view code only."""

        return self.values

    def to_json(self) -> dict[str, object]:
        return {
            "schema": self.schema.descriptor(),
            "source": self.source.to_json(),
            "values": {
                name: _json_atom(self.values[name])
                for name in sorted(self.values)
            },
        }

    @classmethod
    def from_json(
        cls,
        payload: object,
        schema: FactSchema,
        *,
        source: FactSource | None = None,
    ) -> "FactSet":
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "source",
            "values",
        }:
            raise FactValueError("FactSet JSON fields drift")
        if payload["schema"] != schema.descriptor():
            raise FactValueError("FactSet schema drift")
        persisted_source = FactSource.from_json(payload["source"])
        if source is not None and persisted_source != source:
            raise FactValueError("FactSet source drift")
        values = payload["values"]
        if not isinstance(values, dict):
            raise FactValueError("FactSet JSON values must be an object")
        return cls(schema, values, persisted_source)


__all__ = [
    "FactAtom",
    "FactKind",
    "FactSchema",
    "FactSet",
    "FactSource",
    "FactSpec",
]
