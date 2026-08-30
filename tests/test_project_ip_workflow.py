from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.workflows import ip_integration, project_oa
from sigilicon.workflows.project_oa import ProjectOaWorkflow

from conftest import write_component_owner, write_project_context


def _project(tmp_path: Path) -> tuple[Project, Path]:
    project_contract = write_project_context(tmp_path)
    manifest = tmp_path / "ip/fixture/configs/oa.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '''schema = 1
contract_kind = "oa-assembly"
path_scope = "owner"
owner = "fixture"
''',
        encoding="utf-8",
    )
    component = write_component_owner(
        tmp_path,
        "fixture",
        filesets={"oa_source": ("ip/fixture/configs/oa.toml",)},
    )
    return Project.from_file(project_contract), component


def test_project_oa_workflow_exposes_one_public_project(tmp_path: Path) -> None:
    project, _component = _project(tmp_path)

    workflow = ProjectOaWorkflow(project, "fixture")

    assert workflow.project is project
    assert workflow.owner.name == "fixture"


def test_project_oa_workflow_requires_one_owner_assembly(tmp_path: Path) -> None:
    root = tmp_path / "without-oa"
    project_contract = write_project_context(root)
    write_component_owner(root, "rtl-only", filesets={})
    project = Project.from_file(project_contract)

    with pytest.raises(ValueError, match="has no OA assembly"):
        ProjectOaWorkflow(project, "rtl-only")


def test_project_oa_simulation_uses_its_bound_owner_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _component = _project(tmp_path)
    workflow = ProjectOaWorkflow(project, "fixture")
    step = SimpleNamespace(cell="tb_fixture")
    plan = SimpleNamespace(testbenches=(step,))
    result = object()
    planned: list[tuple[Path, Project]] = []

    def plan_rebuild(path: Path, *, project: Project) -> object:
        planned.append((path, project))
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

    monkeypatch.setattr(project_oa, "plan_oa_library_rebuild", plan_rebuild)
    monkeypatch.setattr(project_oa, "run_oa_maestro_testbench", run_testbench)

    assert (
        workflow.simulate(
            testbench="tb_fixture",
            client=object(),
            timeout=17,
        )
        is result
    )
    assert planned == [(tmp_path / "ip/fixture/configs/oa.toml", project)]


def test_project_ip_catalog_reuses_bound_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, component = _project(tmp_path)
    monkeypatch.setattr(
        Project,
        "from_project_root",
        classmethod(
            lambda _cls, _root: pytest.fail("bound workflow reparsed its Project")
        ),
    )

    assert ip_integration.ip_catalog_contract_path(
        project,
        "fixture",
        section="components",
    ) == component


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
        ip_integration,
        "plan_ip_integration_contract",
        lambda contract, **_kwargs: {"ip": contract.name},
    )
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
