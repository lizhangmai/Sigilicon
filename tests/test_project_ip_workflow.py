from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sigilicon.domain.repository import Project
from sigilicon.workflows import ip_integration

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
