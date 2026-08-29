from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.workflows import ip_integration
from sigilicon.workflows.project_ip import ProjectIpWorkflow
from sigilicon.workflows.project_oa import ProjectOaWorkflow
from sigilicon.workflows.xcelium import ProjectXceliumWorkflow

from conftest import write_component_owner, write_project_context


def _project(tmp_path: Path) -> tuple[Project, Path]:
    project_contract = write_project_context(tmp_path)
    component = write_component_owner(tmp_path, "fixture", filesets={})
    return Project.from_file(project_contract), component


def test_project_workflows_expose_one_public_project(tmp_path: Path) -> None:
    project, _component = _project(tmp_path)

    assert ProjectIpWorkflow(project).project is project
    assert ProjectOaWorkflow(project).project is project
    assert ProjectXceliumWorkflow(project).project is project


def test_project_ip_root_aliases_remain_compatible(tmp_path: Path) -> None:
    project, _component = _project(tmp_path)
    workflow = ProjectIpWorkflow(project)

    assert workflow.project_root == project.project_root
    assert workflow.artifact_root == project.artifact_root


def test_project_ip_catalog_reuses_bound_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, component = _project(tmp_path)
    workflow = ProjectIpWorkflow(project)
    monkeypatch.setattr(
        Project,
        "from_project_root",
        classmethod(
            lambda _cls, _root: pytest.fail("bound workflow reparsed its Project")
        ),
    )

    assert workflow.project is project
    assert workflow.contract("fixture", section="components") == component


def test_integration_plan_passes_same_project_to_domain_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, component = _project(tmp_path)
    captured: list[Project] = []

    def load_contract(path: Path, *, project: Project) -> SimpleNamespace:
        captured.append(project)
        return SimpleNamespace(
            project=project,
            project_root=project.project_root,
            path=path,
            owner="fixture",
            name="fixture",
            dependency_lock=None,
            dependencies=(),
            implementation_profiles={},
            variants=(),
        )

    monkeypatch.setattr(ip_integration, "load_ip_integration_contract", load_contract)
    monkeypatch.setattr(
        Project,
        "from_project_root",
        classmethod(
            lambda _cls, _root: pytest.fail("integration plan reparsed its Project")
        ),
    )

    plan = ip_integration.plan_ip_integration(component, project=project)

    assert captured == [project]
    assert plan["ip"] == "fixture"
