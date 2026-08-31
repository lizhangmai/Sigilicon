from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from sigilicon.domain.repository import Project
from sigilicon.workflows import (
    design_lifecycle,
    layout_generation,
    layout_verification,
)
from sigilicon.workflows.layout_generation import LayoutPlanningResult
from sigilicon.workflows.run_artifacts import RunArtifacts

from conftest import write_component_owner


def _project(tmp_path: Path) -> Project:
    write_component_owner(tmp_path, "example", filesets={})
    return Project.from_project_root(tmp_path)


def test_layout_execution_has_no_standalone_wrappers() -> None:
    assert not hasattr(layout_generation, "execute_layout_generation_spec")
    assert not hasattr(layout_verification, "execute_layout_verification_spec")
    assert not hasattr(layout_verification, "execute_layout_verification_set")


def test_managed_layout_generation_reuses_parent_artifacts_and_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    source = SimpleNamespace(text="subckt\n", source_path=tmp_path / "source.scs")
    spec = SimpleNamespace(
        project=project,
        project_root=tmp_path,
        library="example",
        cell="leaf",
        view="layout",
        generator_source=tmp_path / "generator.py",
        generator_dependencies=(),
        generator_modules=(),
        generator_module_sources=(),
        source_snapshot=source,
        source_snapshots=(source,),
        pdk=SimpleNamespace(
            oa=SimpleNamespace(technology_library="example-tech")
        ),
    )
    plan = SimpleNamespace(
        stage="routed",
        instances=(object(), object()),
        canonical_json=lambda: '{"plan":true}\n',
    )
    planning = object.__new__(LayoutPlanningResult)
    object.__setattr__(planning, "spec", spec)
    object.__setattr__(planning, "plan", plan)
    root = tmp_path / "managed-run"
    artifacts = RunArtifacts(
        run_id="managed-run",
        root=root,
        input_root=root / "work/action/inputs",
        work_root=root / "work/action/tool",
        output_root=root / "outputs/action/evidence",
        log_root=root / "logs/action",
        source={},
    )

    class Operation:
        operation_id = "operation-1"
        uncertain_reason = None

        def __init__(self) -> None:
            self.commits = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                for deferred in self.commits:
                    deferred.result = deferred.callback()
                    deferred.completed = True
            return False

        def view_lease(self, *args, **kwargs):
            return nullcontext()

        def mutation_scope(self, *args, **kwargs):
            return nullcontext()

        def require_project_library_target(self, client, library):
            assert library == "example"
            return tmp_path / "workspace/example"

        def defer_commit(self, callback):
            deferred = SimpleNamespace(callback=callback, completed=False)
            self.commits.append(deferred)
            return deferred

        def register_artifact(self, record):
            pytest.fail("managed generation cannot register a nested artifact")

    operation = Operation()
    client = SimpleNamespace(
        library=SimpleNamespace(
            get=lambda library, timeout: SimpleNamespace(
                technology_library="example-tech"
            )
        )
    )
    bound = []
    monkeypatch.setattr(
        layout_generation,
        "workspace_operation",
        lambda *args, **kwargs: operation,
    )
    monkeypatch.setattr(layout_generation, "write_layout_plan", lambda *a, **k: None)
    monkeypatch.setattr(
        layout_generation, "validate_layout_plan", lambda *a, **k: None
    )
    result = layout_generation.generate_layout(
        planning,
        client,
        artifacts=artifacts,
        operation_id="operation-1",
        bind_operation=bound.append,
    )

    assert bound == [operation]
    assert result.instance_count == 2
    assert artifacts.path("outputs", "completion.json").is_file()


def test_design_set_attestation_binds_project_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    paths = (tmp_path / "first.toml", tmp_path / "second.toml")
    client = object()
    inspections: list[tuple[Path, Project]] = []

    def inspect(path: Path, *, project: Project) -> Path:
        inspections.append((path, project))
        return path

    def attest(path: Path, oa_client: object, *, timeout: int) -> dict[str, object]:
        assert oa_client is client
        return {"path": path, "timeout": timeout}

    monkeypatch.setattr(design_lifecycle, "inspect_design", inspect)
    monkeypatch.setattr(design_lifecycle, "attest_oa_design", attest)

    result = design_lifecycle.attest_design_set(
        paths,
        client,
        project=project,
        timeout=17,
    )

    assert inspections == [(path, project) for path in paths]
    assert all(bound is project for _path, bound in inspections)
    assert result == {
        "passed": True,
        "designs": tuple({"path": path, "timeout": 17} for path in paths),
    }
