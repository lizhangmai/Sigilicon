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
