"""Portable, content-addressed source snapshot of one native OpenAccess view."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping

from sigilicon.artifacts import SafeTree, read_json_object, read_nofollow_bytes
from sigilicon.canonical import canonical_digest


@dataclass(frozen=True)
class NativeOaSnapshot:
    library: str
    cell: str
    view: str
    technology_library: str
    files: Mapping[str, bytes]

    def __post_init__(self) -> None:
        for name in (self.library, self.cell, self.view, self.technology_library):
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) is None:
                raise ValueError("native OA snapshot identity must contain OA identifiers")
        if not self.files:
            raise ValueError("native OA snapshot must contain view files")
        for name, content in self.files.items():
            path = PurePosixPath(name)
            if path.is_absolute() or path.as_posix() != name or not path.parts or any(part in {".", ".."} for part in path.parts) or "\\" in name:
                raise ValueError("native OA snapshot member must be a canonical relative path")
            if ".cdslck" in name or not isinstance(content, bytes):
                raise ValueError("native OA snapshot cannot own a lock or non-binary payload")
        if any(parent.as_posix() in self.files for name in self.files for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
            raise ValueError("native OA snapshot member overlaps another file")
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))

    @property
    def record(self) -> dict:
        return {"schema": 1, "contract_kind": "native-oa-snapshot", "library": self.library,
                "cell": self.cell, "view": self.view, "technology_library": self.technology_library,
                "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                           "data": base64.b64encode(data).decode("ascii")} for name, data in sorted(self.files.items())]}

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)

    @classmethod
    def load(cls, path: Path) -> NativeOaSnapshot:
        raw = read_json_object(path, "native OA snapshot")
        if set(raw) != {"schema", "contract_kind", "library", "cell", "view", "technology_library", "files"} or type(raw["schema"]) is not int or raw["schema"] != 1 or raw["contract_kind"] != "native-oa-snapshot":
            raise ValueError("invalid native OA snapshot schema")
        files = {}
        if not isinstance(raw["files"], list):
            raise ValueError("native OA snapshot file inventory is required")
        for row in raw["files"]:
            if not isinstance(row, dict) or set(row) != {"path", "sha256", "data"}:
                raise ValueError("invalid native OA snapshot member")
            if not isinstance(row["path"], str) or not isinstance(row["data"], str):
                raise ValueError("native OA snapshot member path and encoding must be strings")
            data = base64.b64decode(row["data"], validate=True)
            if row["path"] in files or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("native OA snapshot duplicate member or content digest drift")
            files[row["path"]] = data
        return cls(raw["library"], raw["cell"], raw["view"], raw["technology_library"], files)

    @classmethod
    def capture(cls, directory: Path, *, library: str, cell: str, view: str, technology_library: str) -> NativeOaSnapshot:
        tree = SafeTree(directory)
        inventory = tree.inventory()
        files = {name.as_posix(): read_nofollow_bytes(entry.path) for name, entry in inventory.files.items()}
        result = cls(library, cell, view, technology_library, files)
        result.verify(directory)
        return result

    def verify(self, directory: Path) -> None:
        inventory = SafeTree(directory).inventory()
        actual = {name.as_posix(): (entry.size, entry.sha256) for name, entry in inventory.files.items()}
        expected = {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in self.files.items()}
        if actual != expected:
            raise ValueError(f"native OA source content drift: {self.library}/{self.cell}/{self.view}")
