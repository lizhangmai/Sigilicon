"""Explicit, tool-bound Git checkout and source-content evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Mapping

from sigilicon.execution._model import Resources
from sigilicon.external_tools import ProcessRequest, managed_process, owned_input_file


@dataclass(frozen=True)
class CheckoutState:
    """Exact revision and worktree state used by a source release."""

    commit: str
    working_tree_dirty: bool
    changes: tuple[str, ...]


def _git_output(root: Path, resources: Resources, *arguments: str) -> str:
    """Execute one Git query through the managed process boundary."""
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
    }
    with resources.owned_tool("vcs.git") as executable:
        result = managed_process.run(ProcessRequest(
            argv=(*executable.command, "-c", "core.fsmonitor=false", "-c",
                  "core.hooksPath=/dev/null", *arguments),
            executable=executable.executable, cwd=Path(root).resolve(),
            environment=environment, timeout_seconds=30,
            before_spawn=executable.require_visible,
            pass_fds=(executable.target.fd, executable.target.directory_fd),
        ))
    if result.returncode:
        raise RuntimeError(f"Git {arguments[0]} failed: {result.stdout}{result.stderr}")
    return result.stdout


def inspect_checkout(root: Path, resources: Resources) -> CheckoutState:
    """Inspect one checkout with the project-configured Git executable."""
    commit = _git_output(root, resources, "rev-parse", "HEAD").strip()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise RuntimeError("cannot resolve source commit")
    changes = tuple(_git_output(root, resources, "status", "--porcelain=v1", "--untracked-files=all").splitlines())
    return CheckoutState(commit, bool(changes), changes)


def verify_source_commit(
    root: Path, resources: Resources, commit: str, sources: Mapping[Path, Path],
) -> None:
    """Require each sealed source to be the exact regular-file blob in commit.

    Keys name project sources; values are the sealed bytes selected for publication.
    Git status alone cannot prove this for ignored files or index flags.
    """
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        raise ValueError("invalid source commit")
    prefix = _git_output(root, resources, "rev-parse", "--show-prefix").removesuffix("\n")
    tree = _git_output(root, resources, "ls-tree", "-r", "-z", "--full-tree", commit)
    blobs = {}
    for entry in tree.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, identity = metadata.split()
        if kind == "blob" and mode in {"100644", "100755"}:
            blobs[name] = identity
    algorithm = "sha1" if len(commit) == 40 else "sha256"
    for source, sealed in sources.items():
        name = prefix + source.relative_to(root).as_posix()
        expected = blobs.get(name)
        if expected is None:
            raise RuntimeError(f"release source is not a regular file tracked by {commit}: {name}")
        with owned_input_file(sealed) as held:
            size = os.fstat(held.fd).st_size
            digest = hashlib.new(algorithm)
            digest.update(f"blob {size}\0".encode())
            offset = 0
            while data := os.pread(held.fd, 1024 * 1024, offset):
                digest.update(data)
                offset += len(data)
        if offset != size or digest.hexdigest() != expected:
            raise RuntimeError(f"release source content differs from Git commit {commit}: {name}")


__all__ = ["CheckoutState", "inspect_checkout", "verify_source_commit"]
