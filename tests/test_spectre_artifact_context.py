from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import write_project_context
from sigilicon.domain.repository import Project
from sigilicon.workflows.spectre import SpectreArtifactContext


def test_spectre_artifact_context_preserves_an_explicit_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project.from_project_root(tmp_path)
    monkeypatch.setattr(
        Project,
        "from_project_root",
        classmethod(
            lambda cls, root: pytest.fail(
                "an explicit Project must not trigger root rebinding"
            )
        ),
    )

    context = SpectreArtifactContext(
        project=project,
        library="fixture",
        cell="leaf",
        testbench="tb_leaf",
    )

    assert context.project is project
    assert context.project_root == project.project_root
    assert hash(context) == hash(
        SpectreArtifactContext(
            project=project,
            library="fixture",
            cell="leaf",
            testbench="tb_leaf",
        )
    )
    assert "Project(" not in repr(context)
    replaced = replace(context, library="other")
    assert replaced.project is project
    assert replaced.project_root == context.project_root
    assert replaced.library == "other"

    other_root = tmp_path / "other-project"
    other_project = Project.from_file(write_project_context(other_root))
    other_context = SpectreArtifactContext(
        project=other_project,
        library="fixture",
        cell="leaf",
        testbench="tb_leaf",
    )
    assert other_context != context
    assert len({context, other_context}) == 2

    with pytest.raises(ValueError, match="root disagrees with explicit Project"):
        SpectreArtifactContext(
            tmp_path / "other",
            "fixture",
            "leaf",
            "tb_leaf",
            project=project,
        )


def test_spectre_artifact_context_keeps_legacy_positional_root_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project.from_project_root(tmp_path)
    roots: list[Path] = []

    def bind_root(cls: type[Project], root: Path) -> Project:
        roots.append(root)
        return project

    monkeypatch.setattr(Project, "from_project_root", classmethod(bind_root))

    context = SpectreArtifactContext(
        tmp_path,
        "fixture",
        "leaf",
        "tb_leaf",
    )

    assert context.project is project
    assert roots == [tmp_path]
