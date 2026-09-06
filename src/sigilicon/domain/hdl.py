"""Ordered HDL compilation inputs, separate from the full source closure."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
import re
from typing import Mapping

from sigilicon.source import SourceReference

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")

@dataclass(frozen=True)
class HdlCompilation:
    top: str
    sources: tuple[str, ...]
    headers: tuple[str, ...] = ()
    include_dirs: tuple[str, ...] = ()
    defines: tuple[tuple[str, str], ...] = ()

    @classmethod
    def resolve(cls, raw: object, closure: Mapping[SourceReference, str]) -> HdlCompilation:
        if not isinstance(raw, Mapping) or set(raw) - {"top", "sources", "headers", "include_dirs", "defines"}:
            raise ValueError("hdl must declare top and ordered component/source identities")
        def select(field, suffixes):
            items = raw.get(field, ())
            if not isinstance(items, (list, tuple)):
                raise ValueError(f"hdl.{field} must be an array")
            selected = []
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {"component", "source"}:
                    raise ValueError(f"hdl.{field} requires component/source identities")
                path = closure.get(SourceReference(item["component"], item["source"]))
                if path is None or PurePosixPath(path).suffix.lower() not in suffixes:
                    raise ValueError(f"hdl.{field} must select HDL in the step source closure")
                selected.append(path)
            return tuple(selected)
        defines = raw.get("defines", {})
        if not isinstance(defines, Mapping):
            raise ValueError("hdl.defines must be a table")
        result = cls(raw.get("top"), select("sources", {".v", ".sv"}),
                     select("headers", {".vh", ".svh"}), raw.get("include_dirs", ()),
                     tuple(sorted(defines.items())))
        result.validate()
        return cls(result.top, result.sources, result.headers, tuple(result.include_dirs), result.defines)

    def validate(self) -> None:
        if not isinstance(self.top, str) or not _IDENTIFIER.fullmatch(self.top):
            raise ValueError("hdl.top must be a module identifier")
        if not self.sources or len(set(self.sources)) != len(self.sources):
            raise ValueError("hdl.sources must select unique ordered compilation units")
        if len(set(self.headers)) != len(self.headers) or set(self.headers) & set(self.sources):
            raise ValueError("hdl.headers must be unique and separate from compilation units")
        if not isinstance(self.include_dirs, (list, tuple)):
            raise ValueError("hdl.include_dirs must be an array")
        for directory in self.include_dirs:
            if not isinstance(directory, str) or not directory or PurePosixPath(directory).is_absolute() or ".." in PurePosixPath(directory).parts:
                raise ValueError("hdl.include_dirs must stay inside the sealed source tree")
            if not any(PurePosixPath(path).is_relative_to(directory) for path in (*self.sources, *self.headers)):
                raise ValueError("hdl.include_dirs must contain declared HDL inputs")
        if len(set(self.include_dirs)) != len(self.include_dirs):
            raise ValueError("hdl.include_dirs must be unique")
        for name, value in self.defines:
            if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or not isinstance(value, str) or any(c in value for c in "\n\r\x00"):
                raise ValueError("hdl.defines requires identifier names and single-line string values")

    @property
    def record(self) -> dict[str, object]:
        return asdict(self)
