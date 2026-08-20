"""Current live-content checks for generated local OpenAccess views."""

from __future__ import annotations

import hashlib
from pathlib import Path
def _is_transient_oa_file(path: Path) -> bool:
    return ".cdslck" in path.name or path.name.endswith(("%", ".old"))


def oa_view_digest(
    view_dir: Path,
    *,
    allowed_symlink_root: Path | None = None,
) -> str:
    """Hash stable view contents and tightly scoped Cadence source links."""

    if view_dir.is_symlink() or not view_dir.is_dir():
        raise RuntimeError(f"OpenAccess view directory is missing or unsafe: {view_dir}")
    allowed_root = allowed_symlink_root.resolve() if allowed_symlink_root else None
    digest = hashlib.sha256()
    files = 0
    for path in sorted(view_dir.rglob("*")):
        if _is_transient_oa_file(path):
            continue
        if path.is_symlink():
            if allowed_root is None:
                raise RuntimeError(f"refusing symlink inside OpenAccess view: {path}")
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(f"unsafe OpenAccess source symlink: {path}: {exc}") from exc
            if not target.is_relative_to(allowed_root) or not target.is_file():
                raise RuntimeError(
                    f"OpenAccess source symlink leaves the project or is not a file: {path}"
                )
            relative = path.relative_to(view_dir).as_posix().encode()
            link_text = path.readlink().as_posix().encode()
            target_relative = target.relative_to(allowed_root).as_posix().encode()
            content = target.read_bytes()
            for value in (b"symlink", relative, link_text, target_relative, content):
                digest.update(len(value).to_bytes(8, "big"))
                digest.update(value)
            files += 1
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(view_dir).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        files += 1
    if files == 0:
        raise RuntimeError(f"OpenAccess view has no persistent files: {view_dir}")
    return digest.hexdigest()
