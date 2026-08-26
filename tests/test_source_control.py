from __future__ import annotations

from pathlib import Path
import subprocess

from sigilicon.workflows.source_control import inspect_source_state


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

    clean = inspect_source_state(tmp_path).as_dict()
    assert clean["commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert clean["working_tree_dirty"] is False
    assert clean["changes"] == []

    source.write_text("R0 (A B) resistor r=2k\n", encoding="utf-8")
    dirty = inspect_source_state(tmp_path).as_dict()
    assert dirty["commit"] == clean["commit"]
    assert dirty["working_tree_dirty"] is True
    assert dirty["changes"] == [" M source.scs"]
