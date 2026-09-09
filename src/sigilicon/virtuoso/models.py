"""Content-bound model files retained with their OA library, not a build run."""

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from sigilicon.artifacts import SafeTree, _inspect_nofollow_file, copy_immutable_file
from sigilicon.canonical import canonical_digest
from sigilicon.domain.platform import SimulationModelSet
from sigilicon.external_tools import owned_directory, owned_input_closure


@dataclass(frozen=True)
class _ModelFile:
    relative: Path
    source: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class OaModelInputs:
    """The exact model closure selected by the current execution plan."""

    entry: Path
    files: tuple[_ModelFile, ...]

    @classmethod
    def capture(cls, model_set: SimulationModelSet, sealed_paths: Mapping[Path, Path]) -> "OaModelInputs":
        paths = model_set.paths
        missing = set(paths) - sealed_paths.keys()
        if missing:
            raise ValueError(f"OA models are outside the sealed input closure: {sorted(missing)}")
        files = []
        for asset, relative in sorted(model_set.members, key=lambda row: row[1]):
            source = sealed_paths[asset.require_path()]
            metadata, digest = _inspect_nofollow_file(source)
            files.append(_ModelFile(Path(relative), source, metadata.st_size, digest))
        return cls(Path(model_set.members[0][1]), tuple(files))

    @property
    def identity(self) -> str:
        return canonical_digest({
            "entry": self.entry.as_posix(),
            "files": [{"path": item.relative.as_posix(), "size": item.size,
                       "sha256": item.sha256} for item in self.files],
        })

    def directory(self, library: Path) -> Path:
        return library / ".sigilicon-models" / self.identity

    def install(self, library: Path) -> Path:
        """Persist planned bytes in an existing, caller-owned OA library."""
        with owned_directory(library) as held_library:
            root = self.directory(library)
            with owned_directory(root, create_missing=True):
                for item in self.files:
                    target = root / item.relative
                    try:
                        copy_immutable_file(item.source, target,
                                            expected_size=item.size, expected_sha256=item.sha256)
                    except FileExistsError:
                        self._check_file(target, item)
                tree = SafeTree(root)
                if set(tree.inventory().files) != {item.relative for item in self.files}:
                    raise RuntimeError(f"OA model directory contains unplanned files: {root}")
                held_library.require_visible()
        return root / self.entry

    @staticmethod
    def _check_file(path: Path, item: _ModelFile) -> None:
        metadata, digest = _inspect_nofollow_file(path)
        if (metadata.st_size, digest) != (item.size, item.sha256):
            raise RuntimeError(f"OA model contents differ from the current plan: {path}")

    @contextmanager
    def verify(self, library: Path, observations: Sequence[Mapping[str, object]]) -> Iterator[dict[str, object]]:
        """Verify actual references and watch all model/include files until exit."""
        root = self.directory(library)
        expected = {root / item.relative for item in self.files}
        if not observations:
            raise RuntimeError("native OA setup has no model file references")
        for row in observations:
            actual = Path(str(row.get("file", "")))
            if not actual.is_absolute() or actual not in expected:
                raise RuntimeError(
                    f"OA model is not bound to the current plan: {actual}; rebuild the OA testbench"
                )
        with owned_input_closure(root, files=tuple(expected)):
            for item in self.files:
                self._check_file(root / item.relative, item)
            yield {"identity": self.identity, "files": len(self.files), "passed": True}
