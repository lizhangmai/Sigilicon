"""Portable identity of the Python implementation used to compile and execute plans."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from urllib.parse import unquote, urlparse
from importlib import metadata
from pathlib import Path
import re
import sys
from typing import Mapping

from sigilicon.artifacts import read_nofollow_bytes
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import freeze_toml_document, thaw_toml_document


@dataclass(frozen=True)
class SoftwareIdentity:
    document: Mapping

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", freeze_toml_document(dict(self.document)))

    @classmethod
    def capture(cls) -> SoftwareIdentity:
        root = Path(__file__).resolve().parents[1]
        sources = []
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".il"}:
                continue
            payload = read_nofollow_bytes(path)
            sources.append({"path": path.relative_to(root).as_posix(), "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest()})
        packages = {}
        pending = ["sigilicon", "laygo2", "virtuoso-bridge"]
        while pending:
            name = pending.pop().lower().replace("_", "-")
            if name in packages:
                continue
            try:
                distribution = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                continue
            packages[name] = {"version": distribution.version,
                              "metadata_sha256": hashlib.sha256(
                                  (distribution.read_text("METADATA") or "").encode()).hexdigest()}
            direct = json.loads(distribution.read_text("direct_url.json") or "{}")
            if direct.get("vcs_info", {}).get("commit_id"):
                packages[name]["vcs_commit"] = direct["vcs_info"]["commit_id"]
            if name != "sigilicon" and direct.get("dir_info", {}).get("editable"):
                parsed = urlparse(direct.get("url", ""))
                if parsed.scheme == "file":
                    checkout = Path(unquote(parsed.path))
                    tree = checkout / "src" if (checkout / "src").is_dir() else checkout
                    members = [{"path": path.relative_to(tree).as_posix(),
                                "sha256": hashlib.sha256(read_nofollow_bytes(path)).hexdigest()}
                               for path in sorted(tree.rglob("*")) if path.suffix in {".py", ".il"}
                               and not any(part.startswith(".") for part in path.relative_to(tree).parts)]
                    packages[name]["editable_source_identity"] = canonical_digest(members)
            for requirement in distribution.requires or ():
                match = re.match(r"[A-Za-z0-9_.-]+", requirement)
                if match:
                    pending.append(match.group())
        return cls({"python": {"version": sys.version, "implementation": sys.implementation.name,
                               "cache_tag": sys.implementation.cache_tag},
                    "sigilicon_sources": sources, "packages": packages})

    @property
    def record(self) -> dict:
        return thaw_toml_document(self.document)

    @property
    def identity(self) -> str:
        return canonical_digest(self.record)

    def current(self) -> bool:
        return self.identity == self.capture().identity
