from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import json

import pytest

from sigilicon.project import Project
from sigilicon.adapters.cadence import layout_generation
from sigilicon.adapters.cadence import oa_library_execution
from sigilicon.adapters.cadence.oa_library import LayoutRebuildStep, OALibraryRebuildPlan
from sigilicon.adapters.cadence.layout_generation import LayoutPlanningResult
from sigilicon.execution._model import Resources
from sigilicon.execution._workspace import ExecutionWorkspace

from conftest import write_component_owner


def _project(tmp_path: Path) -> Project:
    write_component_owner(tmp_path, "example", filesets={})
    return Project.open(tmp_path)


@pytest.mark.parametrize("through_rebuild", (False, True))
def test_managed_layout_generation_reuses_parent_artifacts_and_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    through_rebuild: bool,
) -> None:
    project = _project(tmp_path)
    source = SimpleNamespace(text="subckt\n", source_path=tmp_path / "source.scs")
    spec = SimpleNamespace(
        project_root=tmp_path,
        workspace_root=project.workspace_root,
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
    artifacts = ExecutionWorkspace(
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
                    if not deferred.completed:
                        deferred.result = deferred.callback()
                        deferred.completed = True
            return False

        def view_lease(self, *args, **kwargs):
            assert bound[-1] is self
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

    operation = Operation()
    client = SimpleNamespace(
        library=SimpleNamespace(
            list=lambda **kwargs: ["example"],
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
    if through_rebuild:
        monkeypatch.setattr(oa_library_execution, "list_cells", lambda *a, **k: {
            "cells": [{"name": "leaf", "views": ["layout"]}],
        })
        monkeypatch.setattr(oa_library_execution, "cell_view_exists", lambda *a, **k: True)
        monkeypatch.setattr(oa_library_execution, "workspace_operation", lambda *a, **k: operation)
        monkeypatch.setattr(oa_library_execution, "validate_layout_plan", lambda *a, **k: None)
        assembly = OALibraryRebuildPlan(
            source=SimpleNamespace(workspace_root=project.workspace_root),
            library="example", cells=("leaf",), designs=(), testbenches=(), views=(),
            layouts=(LayoutRebuildStep(planning, ()),),
            expected_views={"leaf": ("layout",)},
        )
        result = oa_library_execution.rebuild_oa_library(
            assembly, client, source_paths={}, resource_paths={}, resources=Resources(),
            artifacts=artifacts, operation_id="operation-1", bind_operation=bound.append,
        )
        assert result["passed"] is True
    else:
        result = layout_generation.generate_layout(
            planning, client, artifacts=artifacts, operation_id="operation-1",
            bind_operation=bound.append,
        )
        assert result.instance_count == 2

    completions = list(artifacts.output_root.rglob("completion.json"))
    assert len(completions) == 1
    completion = json.loads(completions[0].read_text())
    assert completion["cell"] == "leaf"
    assert completion["instance_count"] == 2
