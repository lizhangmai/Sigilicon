from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.workflows import oa_simulation


def test_named_oa_simulation_reuses_an_explicit_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project.from_project_root(tmp_path)
    manifest = tmp_path / "ip/example/configs/oa.toml"
    step = SimpleNamespace(cell="tb_fixture")
    plan = SimpleNamespace(testbenches=(step,))
    result = object()
    planned: list[tuple[Path, Project, str]] = []

    def plan_rebuild(
        path: Path,
        *,
        project: Project,
        library: str,
    ) -> object:
        planned.append((path, project, library))
        return plan

    def run_testbench(
        selected_plan: object,
        selected_step: object,
        client: object,
        *,
        timeout: int,
    ) -> object:
        assert selected_plan is plan
        assert selected_step is step
        assert timeout == 17
        return result

    monkeypatch.setattr(oa_simulation, "plan_oa_library_rebuild", plan_rebuild)
    monkeypatch.setattr(oa_simulation, "run_oa_maestro_testbench", run_testbench)

    assert (
        oa_simulation.run_named_oa_maestro_testbench(
            manifest,
            project=project,
            library="fixture",
            testbench="tb_fixture",
            client=object(),
            timeout=17,
        )
        is result
    )
    assert planned == [(manifest, project, "fixture")]

    with pytest.raises(ValueError, match="root disagrees with explicit Project"):
        oa_simulation.run_named_oa_maestro_testbench(
            manifest,
            project=project,
            project_root=tmp_path / "other",
            library="fixture",
            testbench="tb_fixture",
            client=object(),
        )


def test_named_oa_simulation_keeps_the_root_only_compatibility_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project.from_project_root(tmp_path)
    step = SimpleNamespace(cell="tb_fixture")
    plan = SimpleNamespace(testbenches=(step,))
    roots: list[Path] = []

    def bind_root(cls: type[Project], root: Path) -> Project:
        roots.append(root)
        return project

    monkeypatch.setattr(Project, "from_project_root", classmethod(bind_root))
    monkeypatch.setattr(
        oa_simulation,
        "plan_oa_library_rebuild",
        lambda path, *, project, library: plan,
    )
    monkeypatch.setattr(
        oa_simulation,
        "run_oa_maestro_testbench",
        lambda selected_plan, selected_step, client, *, timeout: object(),
    )

    oa_simulation.run_named_oa_maestro_testbench(
        tmp_path / "oa.toml",
        project_root=tmp_path,
        library="fixture",
        testbench="tb_fixture",
        client=object(),
    )

    assert roots == [tmp_path]
