from __future__ import annotations

from pathlib import Path
import subprocess
import pytest

from sigilicon.execution._resources import Resources
from sigilicon.adapters.release.source_control import inspect_checkout, verify_source_commit


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return completed.stdout.strip()


def test_source_state_uses_git_revision_and_worktree_status(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "Sigilicon Test")
    _git(tmp_path, "config", "user.email", "sigilicon@example.invalid")
    source = tmp_path / "source.scs"
    source.write_text("R0 (A B) resistor r=1k\n", encoding="utf-8")
    _git(tmp_path, "add", "--all")
    _git(tmp_path, "commit", "--quiet", "-m", "fixture")

    resources = Resources(tools={"vcs.git": "/usr/bin/git"})
    clean = inspect_checkout(tmp_path, resources)
    assert clean.commit == _git(tmp_path, "rev-parse", "HEAD")
    assert clean.working_tree_dirty is False
    assert clean.changes == ()

    source.write_text("R0 (A B) resistor r=2k\n", encoding="utf-8")
    dirty = inspect_checkout(tmp_path, resources)
    assert dirty.commit == clean.commit
    assert dirty.working_tree_dirty is True
    assert dirty.changes == (" M source.scs",)


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("directory", ("project", " leading and trailing "))
def test_source_commit_verification_supports_nested_projects(tmp_path: Path, object_format: str, directory: str) -> None:
    _git(tmp_path, "init", "--quiet", f"--object-format={object_format}")
    _git(tmp_path, "config", "user.name", "Sigilicon Test")
    _git(tmp_path, "config", "user.email", "sigilicon@example.invalid")
    root = tmp_path / directory
    root.mkdir()
    source = root / "design.scs"
    source.write_bytes(b"binary source\x00\xff\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "--quiet", "-m", "source")
    resources = Resources(tools={"vcs.git": "/usr/bin/git"})
    commit = inspect_checkout(root, resources).commit
    sealed = tmp_path / "sealed"
    sealed.write_bytes(source.read_bytes())

    verify_source_commit(root, resources, commit, {source: sealed})
    sealed.write_bytes(b"different source")
    with pytest.raises(RuntimeError, match="differs from Git commit"):
        verify_source_commit(root, resources, commit, {source: sealed})
