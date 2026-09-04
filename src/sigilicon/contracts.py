"""Strict common envelope primitives for domain-owned TOML contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import os
from pathlib import Path, PurePosixPath
import tomllib
from types import MappingProxyType
from typing import Any, Iterable, Mapping, TypeVar, cast

from sigilicon.artifacts import read_nofollow_text

CONFIG_SCHEMA = 1
_CONFIG_SCHEMAS = {
    "ip-dependency-lock": 3,
    "ip-component": 3,
    "ip-release": 2,
    "owner-operations": 3,
}
PATH_SCOPES = frozenset(
    {"repository", "owner", "cell", "verification", "platform", "variant"}
)
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_MISSING = object()
_T = TypeVar("_T")


class ContractReader:
    """Consume one strict TOML table without duplicating scalar validation."""

    __slots__ = ("_label", "_raw", "_remaining")

    def __init__(self, raw: Mapping[str, Any], label: str) -> None:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} must be a TOML table")
        if not isinstance(label, str) or not label:
            raise ValueError("contract reader label must be non-empty")
        self._raw = raw
        self._label = label
        self._remaining = set(raw)

    @property
    def raw(self) -> Mapping[str, Any]:
        return self._raw

    def take(self, name: str, default: _T | object = _MISSING) -> Any | _T:
        self._remaining.discard(name)
        if name in self._raw:
            return self._raw[name]
        if default is _MISSING:
            raise ValueError(f"{self._label}.{name} is required")
        return cast(_T, default)

    def text(
        self,
        name: str,
        default: str | None | object = _MISSING,
    ) -> str | None:
        value = self.take(name, default)
        if value is None and default is None:
            return None
        return require_text(value, f"{self._label}.{name}")

    def boolean(self, name: str, default: bool | object = _MISSING) -> bool:
        value = self.take(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"{self._label}.{name} must be boolean")
        return value

    def integer(
        self,
        name: str,
        default: int | None | object = _MISSING,
        *,
        minimum: int | None = None,
    ) -> int | None:
        value = self.take(name, default)
        if value is None and default is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{self._label}.{name} must be an integer")
        if minimum is not None and value < minimum:
            raise ValueError(
                f"{self._label}.{name} must be at least {minimum}"
            )
        return value

    def table(
        self,
        name: str,
        default: Mapping[str, Any] | object = _MISSING,
    ) -> Mapping[str, Any]:
        return require_table(self.take(name, default), f"{self._label}.{name}")

    def strings(
        self,
        name: str,
        default: tuple[str, ...] | object = _MISSING,
        *,
        nonempty: bool = False,
        unique: bool = True,
    ) -> tuple[str, ...]:
        return require_strings(
            self.take(name, default),
            f"{self._label}.{name}",
            nonempty=nonempty,
            unique=unique,
        )

    def consume(self, *names: str) -> None:
        self._remaining.difference_update(names)

    def finish(self) -> None:
        if self._remaining:
            raise ValueError(
                f"{self._label} contains unknown fields: "
                f"{sorted(self._remaining)}"
            )


def require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def require_table(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a TOML table")
    return value


def require_strings(
    value: object,
    field: str,
    *,
    nonempty: bool = False,
    unique: bool = True,
) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        qualifier = "a non-empty " if nonempty else "a "
        raise ValueError(f"{field} must be {qualifier}string array")
    result = tuple(value)
    if unique and len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicates")
    return result


def require_relative_path(value: object, field: str) -> PurePosixPath:
    text = require_text(value, field)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or "\\" in text
        or path.as_posix() != text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field} must be a safe relative path: {text!r}")
    return path


def freeze_toml_document(value: Any) -> Any:
    """Recursively freeze one parsed TOML value for source snapshots."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_toml_document(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_toml_document(item) for item in value)
    return value


def is_frozen_toml_document(value: object) -> bool:
    """Return whether one parsed TOML value is recursively immutable."""

    if isinstance(value, _MAPPING_PROXY_TYPE):
        return all(
            isinstance(key, str) and is_frozen_toml_document(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return all(is_frozen_toml_document(item) for item in value)
    return isinstance(value, (str, int, float, bool, datetime, date, time))


def thaw_toml_document(value: Any) -> Any:
    """Copy a frozen TOML value back to the parser's dict/list shape."""

    if isinstance(value, Mapping):
        return {key: thaw_toml_document(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_toml_document(item) for item in value]
    return value


@dataclass(frozen=True)
class ConfigHeader:
    schema: int
    contract_kind: str
    path_scope: str
    owner: str


@dataclass(frozen=True)
class DocumentStore:
    """One immutable, no-follow TOML closure below a project root."""

    root: Path
    documents: Mapping[Path, Mapping[str, Any]]

    def __post_init__(self) -> None:
        root = self.root.resolve()
        checked: dict[Path, Mapping[str, Any]] = {}
        for path, document in self.documents.items():
            resolved = path.resolve()
            if (
                path != resolved
                or not resolved.is_relative_to(root)
                or not is_frozen_toml_document(document)
            ):
                raise ValueError(f"document store source identity drift: {path}")
            checked[resolved] = document
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "documents", MappingProxyType(checked))

    @classmethod
    def capture(cls, root: Path, paths: Iterable[Path]) -> "DocumentStore":
        project_root = root.resolve()
        documents: dict[Path, Mapping[str, Any]] = {}
        for path in sorted(set(paths)):
            resolved = path.resolve()
            if (
                path != resolved
                or not resolved.is_relative_to(project_root)
                or path.suffix != ".toml"
            ):
                raise ValueError(f"configuration source is unsafe: {path}")
            try:
                raw = tomllib.loads(read_nofollow_text(resolved))
            except (
                OSError,
                RuntimeError,
                UnicodeError,
                tomllib.TOMLDecodeError,
            ) as exc:
                raise ValueError(f"cannot read TOML {path}: {exc}") from exc
            documents[resolved] = freeze_toml_document(raw)
        return cls(project_root, documents)

    @classmethod
    def capture_trees(
        cls,
        root: Path,
        trees: Iterable[Path],
        *,
        paths: Iterable[Path] = (),
    ) -> "DocumentStore":
        """Capture TOML files below exact non-symlink directory roots."""

        selected = set(paths)
        pending = list(trees)
        while pending:
            current = pending.pop()
            if current != current.resolve() or not current.is_dir():
                raise ValueError(
                    f"configuration owner root is missing or unsafe: {current}"
                )
            try:
                with os.scandir(current) as scanned:
                    entries = sorted(scanned, key=lambda entry: entry.name)
            except OSError as exc:
                raise ValueError(
                    f"cannot inspect configuration owner root {current}: {exc}"
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        if path.suffix == ".toml" or entry.is_dir():
                            raise ValueError(
                                f"configuration source path is a symlink: {path}"
                            )
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if path.suffix == ".toml":
                            selected.add(path)
                    elif path.suffix == ".toml":
                        raise ValueError(
                            f"configuration source must be a regular file: {path}"
                        )
                except OSError as exc:
                    raise ValueError(
                        f"cannot inspect configuration source {path}: {exc}"
                    ) from exc
        return cls.capture(root, selected)

    def resolve(self, path: Path) -> Mapping[str, Any]:
        resolved = path.resolve()
        if path != resolved:
            raise ValueError("document store lookup must be resolved")
        try:
            return self.documents[resolved]
        except KeyError as exc:
            raise ValueError(
                f"configuration source is outside the captured store: {path}"
            ) from exc

    def verify(
        self,
        label: str,
        expected: Mapping[Path, Mapping[str, Any]],
    ) -> None:
        if not isinstance(label, str) or not label:
            raise ValueError("document store label must be non-empty")
        for path, document in expected.items():
            if not is_frozen_toml_document(document):
                raise ValueError(f"{label} must be frozen: {path}")
            if self.resolve(path) != document:
                raise ValueError(f"{label} disagrees with captured source: {path}")

    def verify_current(
        self,
        label: str,
        paths: Iterable[Path] | None = None,
    ) -> None:
        """Verify selected source files against this immutable capture."""

        selected = tuple(self.documents) if paths is None else tuple(paths)
        for path in selected:
            expected = self.resolve(path)
            try:
                current = freeze_toml_document(read_toml(path))
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"{label} source document drift: {path}") from exc
            if current != expected:
                raise ValueError(f"{label} source document drift: {path}")


def contract_schema(contract_kind: str) -> int:
    """Return the one supported schema for a typed configuration domain."""

    return _CONFIG_SCHEMAS.get(contract_kind, CONFIG_SCHEMA)


def require_config_header(
    raw: Mapping[str, Any],
    path: Path,
    *,
    contract_kind: str | tuple[str, ...],
    path_scope: str | tuple[str, ...],
    owner: str | None = None,
    schema: int = CONFIG_SCHEMA,
) -> ConfigHeader:
    """Validate the common header without flattening the domain payload."""

    actual_schema = raw.get("schema")
    if isinstance(actual_schema, bool) or actual_schema != schema:
        raise ValueError(f"{path}: schema must be {schema}")
    actual_kind = require_text(raw.get("contract_kind"), f"{path}: contract_kind")
    allowed_kinds = (contract_kind,) if isinstance(contract_kind, str) else contract_kind
    if actual_kind not in allowed_kinds:
        raise ValueError(
            f"{path}: contract_kind must be one of {sorted(allowed_kinds)}"
        )
    actual_scope = require_text(raw.get("path_scope"), f"{path}: path_scope")
    allowed_scopes = (path_scope,) if isinstance(path_scope, str) else path_scope
    if actual_scope not in allowed_scopes or actual_scope not in PATH_SCOPES:
        raise ValueError(
            f"{path}: path_scope must be one of "
            f"{sorted(set(allowed_scopes) & PATH_SCOPES)}"
        )
    actual_owner = require_text(raw.get("owner"), f"{path}: owner")
    if owner is not None and actual_owner != owner:
        raise ValueError(f"{path}: owner must be {owner!r}, got {actual_owner!r}")
    return ConfigHeader(schema, actual_kind, actual_scope, actual_owner)


def read_toml(path: Path) -> dict[str, Any]:
    """Read one TOML document and require a table root."""

    try:
        value = tomllib.loads(read_nofollow_text(path))
    except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"TOML root must be a table: {path}")
    return value


__all__ = [
    "CONFIG_SCHEMA",
    "ContractReader",
    "ConfigHeader",
    "DocumentStore",
    "PATH_SCOPES",
    "contract_schema",
    "freeze_toml_document",
    "is_frozen_toml_document",
    "read_toml",
    "require_config_header",
    "require_relative_path",
    "require_strings",
    "require_table",
    "require_text",
    "thaw_toml_document",
]
