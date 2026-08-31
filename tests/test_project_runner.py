from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.domain.repository import Project
from sigilicon.flow import (
    ActionContract,
    AdapterResult,
    CollectedActionResult,
    ExecutionEnvironment,
    FlowExecutionError,
    FlowRegistry,
    SourceMember,
)
from sigilicon.flow.circuit_design import (
    DESIGN_ACTION_PLAN,
    DESIGN_SOURCE_CHECK_ACTION,
)
from sigilicon.flow.source_assets import snapshot_source_member
from sigilicon.workflows import design_flow
from sigilicon.workflows import project_runner as project_runner_module
from sigilicon.workflows.project_runner import (
    ProjectExecution,
    ProjectRunner,
    resolve_project_execution,
)

from conftest import write_component_owner, write_project_context


_RECIPE = """schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
name = "smoke-recipe"

[actions."circuit-design.source-check"]
adapter = "fake-source-check"

[[nodes]]
id = "check"
action = "circuit-design.source-check"
config = { target = "smoke", mode = "check" }

[[nodes]]
id = "audit"
action = "circuit-design.source-check"
config = { target = "smoke", mode = "audit" }
"""

_TARGETS = """schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.smoke]
description = "Offline smoke target"

[targets.smoke.operations.check]
recipe = "configs/smoke.toml"
goals = ["check"]

[targets.smoke.operations.all]
recipe = "configs/smoke.toml"
goals = ["check", "audit"]
"""


def _write_project(root: Path) -> tuple[Project, Path, Path, Path]:
    """Create one owner target catalog and one owner execution recipe."""

    write_project_context(root)
    owner_root = root / "ip/example"
    flow_root = owner_root / "configs"
    flow_root.mkdir(parents=True, exist_ok=True)
    recipe = flow_root / "smoke.toml"
    recipe.write_text(_RECIPE, encoding="utf-8")
    targets = flow_root / "targets.toml"
    targets.write_text(_TARGETS, encoding="utf-8")
    implementation = owner_root / "implementation.py"
    implementation.write_text("VALUE = 1\n", encoding="utf-8")

    component = write_component_owner(
        root,
        "example",
        filesets={
            "flow": (
                "ip/example/configs/targets.toml",
                "ip/example/configs/smoke.toml",
            ),
            "support": ("ip/example/implementation.py",),
        },
    )
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            "\n[filesets]\n",
            '\ntarget_catalog = "ip/example/configs/targets.toml"\n\n[filesets]\n',
        ),
        encoding="utf-8",
    )
    return Project.from_project_root(root), targets, recipe, implementation


class _SimpleDesignPlan:
    def __init__(self, source_members: tuple[SourceMember, ...]) -> None:
        self.source_members = source_members

    def as_dict(self) -> dict[str, object]:
        return {"planned": True}


class _SimpleActionAdapter:
    def run(self, context):
        context.require_action_plan(DESIGN_ACTION_PLAN, _SimpleDesignPlan)
        return AdapterResult.succeeded(
            CollectedActionResult(facts={"passed": True})
        )


def _install_simple_design_seam(
    monkeypatch: pytest.MonkeyPatch,
    project: Project,
    implementation: Path,
) -> None:
    """Use a fake typed design Action while exercising ProjectRunner itself."""

    implementation_member = snapshot_source_member(
        implementation,
        source_root=project.project_root,
        source_label="fixture implementation",
    )

    def plan_design_action(
        selected_project: Project,
        owner: str,
        config,
    ) -> _SimpleDesignPlan:
        assert selected_project is project
        assert owner == "example"
        assert config["target"] == "smoke"
        return _SimpleDesignPlan((implementation_member,))

    monkeypatch.setattr(design_flow, "plan_design_action", plan_design_action)

    def registry(
        selected_project: Project,
        owner,
        **_kwargs,
    ) -> FlowRegistry:
        assert selected_project is project
        assert owner.name == "example"
        result = FlowRegistry()
        result.register_action(
            ActionContract(
                kind=DESIGN_SOURCE_CHECK_ACTION,
                facts=("passed",),
                adapters=("fake-source-check",),
                plan_input_kind=DESIGN_ACTION_PLAN,
            )
        )
        result.register_adapter(
            "fake-source-check",
            _SimpleActionAdapter(),
        )
        return result

    monkeypatch.setattr(project_runner_module, "_project_workflow_registry", registry)


def test_project_runner_targets_describe_and_plan_use_owner_operation_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _targets, _recipe, implementation = _write_project(tmp_path)
    _install_simple_design_seam(monkeypatch, project, implementation)
    runner = ProjectRunner(project, "example")

    assert runner.targets() == (
        {
            "name": "smoke",
            "description": "Offline smoke target",
            "operations": ("check", "all"),
        },
    )
    assert runner.describe("smoke") == {
        "name": "smoke",
        "description": "Offline smoke target",
        "operations": ("check", "all"),
    }
    assert runner.describe("smoke", "check") == {
        "schema": 1,
        "contract_kind": "target-operation-summary",
        "owner": "example",
        "target": "smoke",
        "operation": "check",
        "recipe": "smoke-recipe",
        "nodes": ("check", "audit"),
        "policies": (),
    }

    execution = runner.plan("smoke", "check")

    assert isinstance(execution, ProjectExecution)
    assert execution.owner == "example"
    assert execution.target == "smoke"
    assert execution.operation == "check"
    assert execution.recipe == "smoke-recipe"
    assert execution.plan_identity == "example:smoke:check"
    assert execution.node_count == 1
    assert execution.graph == (("check", ()),)


def test_project_execution_owns_preflight_run_restore_read_and_clean_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _targets, _recipe, implementation = _write_project(tmp_path)
    _install_simple_design_seam(monkeypatch, project, implementation)
    execution = ProjectRunner(project, "example").plan("smoke", "all")
    environment = ExecutionEnvironment()

    assert execution.execution_capabilities == (
        "execute-derived",
        "execute-derived",
    )
    preflight = execution.preflight(environment)
    assert preflight.status == "ready"
    assert all(check.status == "available" for check in preflight.checks)

    progress = []
    run_id = "a" * 32
    result = execution.run(
        environment,
        run_id=run_id,
        progress=progress.append,
    )

    assert result.status == "accepted"
    assert result.owner == "example"
    assert result.flow_id == "smoke"
    assert result.target == "all"
    assert set(result.nodes) == {"check", "audit"}
    assert [item.status for item in progress][0] == "running"
    assert progress[-1].status == "accepted"
    assert execution.read_result(run_id)["status"] == "accepted"

    restored = execution.restore_result(run_id)
    assert restored.status == "accepted"
    assert restored.run_id == run_id
    assert set(restored.nodes) == {"check", "audit"}

    execution.clean(run_id)
    assert not result.run_root.exists()
    with pytest.raises(FlowExecutionError):
        execution.read_result(run_id)


def test_project_execution_identity_is_owner_target_operation_without_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _targets, _recipe, implementation = _write_project(tmp_path)
    _install_simple_design_seam(monkeypatch, project, implementation)

    resolved = resolve_project_execution(project, "example:smoke:check")

    assert resolved.plan_identity == "example:smoke:check"
    assert resolved.owner == "example"
    assert resolved.target == "smoke"
    assert resolved.operation == "check"

    for identity in (
        "example:smoke:check:extra",
        "example:smoke:unknown",
        "other:smoke:check",
    ):
        with pytest.raises(ValueError):
            resolve_project_execution(project, identity)


def test_project_runner_retains_exact_target_and_recipe_sources_for_preflight_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, targets, recipe, implementation = _write_project(tmp_path)
    _install_simple_design_seam(monkeypatch, project, implementation)
    execution = ProjectRunner(project, "example").plan("smoke", "check")

    source_identity = {
        (source["scope"], source["path"])
        for source in execution.record["source_members"]
    }
    assert source_identity == {
        ("project", "ip/example/configs/targets.toml"),
        ("project", "ip/example/configs/smoke.toml"),
    }
    action_plan = execution.record["nodes"][0]["action_plan"]
    assert {
        (source["scope"], source["path"])
        for source in action_plan["sources"]
    } == {("project", "ip/example/implementation.py")}

    for source in (targets, recipe):
        original = source.read_text(encoding="utf-8")
        source.write_text(original + "\n# source drift\n", encoding="utf-8")
        try:
            preflight = execution.preflight(ExecutionEnvironment())
            assert preflight.status == "blocked"
            assert any(
                check.requirement_kind == "target-source"
                and check.status == "changed"
                for check in preflight.checks
            )
        finally:
            source.write_text(original, encoding="utf-8")


def test_project_runner_has_no_legacy_request_flow_execution_or_profile_catalog_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _targets, _recipe, implementation = _write_project(tmp_path)
    _install_simple_design_seam(monkeypatch, project, implementation)
    runner = ProjectRunner(project, "example")
    execution = runner.plan("smoke", "check")

    assert set(project_runner_module.__all__) == {
        "ProjectRunner",
        "ProjectExecution",
        "resolve_project_execution",
    }
    assert not hasattr(project_runner_module, "RunRequest")
    assert not hasattr(project_runner_module, "FlowExecution")
    assert not hasattr(ProjectRunner, "catalog")
    assert not hasattr(ProjectRunner, "profile")
    assert not hasattr(execution, "flow")
    assert not hasattr(execution, "profile")
    assert not hasattr(execution, "plan")
    assert not hasattr(execution, "engine")
