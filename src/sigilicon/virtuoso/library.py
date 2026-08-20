"""Project-facing library reconciliation built on bridge library APIs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any
import uuid

from sigilicon.virtuoso.capability import dispatch_oa_mutation
from sigilicon.virtuoso.confirmation import require_bridge_confirmation


@dataclass(frozen=True)
class LibrarySyncResult:
    action: str
    library: str
    path: Path
    technology_library: str
    cds_entry: str


@dataclass(frozen=True)
class LibraryCreateResult:
    action: str
    library: str
    path: Path
    technology_library: str | None
    cds_entry: str | None


def _sync_cds_lib_entry(library: str, lib_path: Path, cds_lib: Path) -> str:
    """Write one exact, portable ``DEFINE`` entry and return its action."""

    parent_fd = os.open(
        cds_lib.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    source_fd: int | None = None
    temporary_fd: int | None = None
    temporary_name: str | None = None
    try:
        try:
            source_fd = os.open(
                cds_lib.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"cds.lib not found: {cds_lib}") from exc
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"cds.lib is not a single-link regular file: {cds_lib}")
        chunks: list[bytes] = []
        while chunk := os.read(source_fd, 1024 * 1024):
            chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        after_read = os.fstat(source_fd)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_size,
            after_read.st_mtime_ns,
        ):
            raise RuntimeError(f"cds.lib changed while it was read: {cds_lib}")
        os.close(source_fd)
        source_fd = None

        try:
            relative = lib_path.relative_to(cds_lib.parent)
            preferred = f"./{relative.as_posix()}"
        except ValueError:
            preferred = str(lib_path)
        prefix = f"DEFINE {library} "
        lines = text.splitlines()
        matches = [
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith(prefix)
        ]
        coalesced = False
        if len(matches) > 1:
            expected = Path(os.path.abspath(lib_path))
            for index in matches:
                fields = lines[index].strip().split(maxsplit=2)
                if len(fields) != 3:
                    raise RuntimeError(
                        f"cds.lib contains duplicate DEFINE entries for {library}"
                    )
                candidate = Path(fields[2])
                if not candidate.is_absolute():
                    candidate = cds_lib.parent / candidate
                if Path(os.path.abspath(candidate)) != expected:
                    raise RuntimeError(
                        f"cds.lib contains duplicate DEFINE entries for {library}"
                    )
            for index in reversed(matches[1:]):
                del lines[index]
            matches = matches[:1]
            coalesced = True
        replacement = f"{prefix}{preferred}"
        if matches:
            index = matches[0]
            action = (
                "unchanged"
                if not coalesced and lines[index].strip() == replacement
                else "normalized"
            )
            lines[index] = replacement
        else:
            lines.append(replacement)
            action = "added"
        payload = ("\n".join(lines) + "\n").encode("utf-8")

        temporary_name = f".{cds_lib.name}.{uuid.uuid4().hex}.tmp"
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            stat.S_IMODE(metadata.st_mode),
            dir_fd=parent_fd,
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(temporary_fd, remaining)
            if written <= 0:
                raise RuntimeError(f"could not update cds.lib: {cds_lib}")
            remaining = remaining[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        visible = os.stat(
            cds_lib.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (visible.st_dev, visible.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(f"cds.lib identity changed before replace: {cds_lib}")
        os.replace(
            temporary_name,
            cds_lib.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
        return f"{action}: {replacement}"
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def create_project_library(
    client: Any,
    *,
    library: str,
    path: Path,
    technology_library: str | None,
    cds_lib: Path,
    if_missing: bool,
    operation: Any,
) -> LibraryCreateResult:
    expected_path = Path(os.path.abspath(path))

    def dispatch(phase: str, callback: Any) -> Any:
        return dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=None,
            phase=phase,
            callback=callback,
        )

    def sync_cds(phase: str) -> str:
        def write() -> str:
            target = operation.require_project_file_target(
                client,
                cds_lib,
                label="project cds.lib",
            )
            return _sync_cds_lib_entry(library, expected_path, target)

        return dispatch(phase, write)

    visible = client.library.list()
    if library in visible:
        if not if_missing:
            raise RuntimeError(f"library already exists: {library}")
        info = client.library.get(library, timeout=30)
        actual_path = operation.require_project_library_target(client, library)
        if actual_path != expected_path:
            raise RuntimeError(
                f"library {library} resolves to {actual_path}, expected {expected_path}"
            )
        if (
            technology_library is not None
            and info.technology_library != technology_library
        ):
            raise RuntimeError(
                f"library {library} uses technology {info.technology_library}, "
                f"expected {technology_library}"
            )
        return LibraryCreateResult(
            action="exists",
            library=library,
            path=actual_path,
            technology_library=info.technology_library,
            cds_entry=sync_cds("update cds.lib for existing project library"),
        )
    dispatch(
        "create project library directory",
        lambda: operation.ensure_project_directory_target(
            client,
            expected_path,
            label=f"library directory {library}",
        ),
    )

    def create_library() -> Any:
        return dispatch(
            "dispatch project library creation",
            lambda: client.library.create(
                library,
                str(expected_path),
                technology_library=technology_library,
                timeout=120,
            ),
        )

    info = require_bridge_confirmation(
        operation,
        f"create library {library}",
        create_library,
    )
    actual_path = operation.require_project_library_target(client, library)
    if actual_path != expected_path:
        raise RuntimeError(
            f"created library {library} resolves to {actual_path}, "
            f"expected {expected_path}"
        )
    return LibraryCreateResult(
        action="created",
        library=library,
        path=actual_path,
        technology_library=info.technology_library,
        cds_entry=sync_cds("update cds.lib for created project library"),
    )


def ensure_project_library(
    client: Any,
    *,
    library: str,
    path: Path,
    technology_library: str,
    cds_lib: Path,
    operation: Any,
    timeout: int = 120,
) -> LibrarySyncResult:
    """Create or verify a project library and its technology binding."""

    expected_path = Path(os.path.abspath(path))

    def dispatch(phase: str, callback: Any) -> Any:
        return dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=None,
            phase=phase,
            callback=callback,
        )

    def sync_cds(phase: str) -> str:
        def write() -> str:
            target = operation.require_project_file_target(
                client,
                cds_lib,
                label="project cds.lib",
            )
            return _sync_cds_lib_entry(library, expected_path, target)

        return dispatch(phase, write)

    visible = client.library.list(timeout=30)
    if library not in visible:
        dispatch(
            "create missing project library directory",
            lambda: operation.ensure_project_directory_target(
                client,
                expected_path,
                label=f"library directory {library}",
            ),
        )

        def create_library() -> Any:
            return dispatch(
                "dispatch missing project library creation",
                lambda: client.library.create(
                    library,
                    str(expected_path),
                    technology_library=technology_library,
                    timeout=timeout,
                ),
            )

        info = require_bridge_confirmation(
            operation,
            f"create library {library}",
            create_library,
        )
        action = "created"
    else:
        info = client.library.get(library, timeout=30)
        actual_path = operation.require_project_library_target(client, library)
        if actual_path != expected_path:
            raise RuntimeError(
                f"library {library} resolves to {actual_path}, expected {expected_path}"
            )
        if info.technology_library is None:
            def bind_technology() -> Any:
                return dispatch(
                    "dispatch project technology binding",
                    lambda: client.library.set_technology_library(
                        library,
                        technology_library,
                        timeout=timeout,
                    ),
                )

            require_bridge_confirmation(
                operation,
                f"bind technology for library {library}",
                bind_technology,
            )
            info = require_bridge_confirmation(
                operation,
                f"confirm technology for library {library}",
                lambda: client.library.get(library, timeout=30),
            )
            action = "bound-technology"
        elif info.technology_library != technology_library:
            raise RuntimeError(
                f"library {library} uses technology {info.technology_library}, "
                f"but spec requires {technology_library}"
            )
        else:
            action = "verified"
    cds_entry = sync_cds("update cds.lib for project library")
    actual_path = operation.require_project_library_target(client, library)
    if actual_path != expected_path:
        raise RuntimeError(
            f"library {library} resolves to {actual_path}, expected {expected_path}"
        )
    return LibrarySyncResult(
        action=action,
        library=library,
        path=actual_path,
        technology_library=str(info.technology_library),
        cds_entry=cds_entry,
    )
