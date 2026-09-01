from __future__ import annotations

from pathlib import Path
import re

import pytest

from sigilicon.domain.repository import Project
from sigilicon.flow import (
    ActionPlan,
    ActionContract,
    AdapterResult,
    CollectedActionResult,
    ExecutionEnvironment,
    FactKind,
    FactSchema,
    FactSet,
    FactSource,
    FactSpec,
    FlowRegistry,
)
from sigilicon.execution import RunStoreError
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
    def as_dict(self) -> dict[str, object]:
        return {"planned": True}


class _SimpleActionAdapter:
    def run(self, context):
        context.require_action_plan(DESIGN_ACTION_PLAN, _SimpleDesignPlan)
        return AdapterResult.succeeded(
            CollectedActionResult(
                facts=FactSet(
                    context.action.fact_schema,
                    {"passed": True},
                    FactSource(context.action.kind, context.node_id),
                )
            )
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
        owner,
        config,
    ) -> ActionPlan:
        assert selected_project is project
        assert owner.name == "example"
        assert config["target"] == "smoke"
        planned = _SimpleDesignPlan()
        return ActionPlan(
            DESIGN_ACTION_PLAN,
            planned,
            planned.as_dict(),
            (implementation_member,),
        )

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
                fact_schema=FactSchema(
                    DESIGN_SOURCE_CHECK_ACTION,
                    (FactSpec("passed", FactKind.BOOLEAN),),
                ),
                adapters=("fake-source-check",),
                plan_input_kind=DESIGN_ACTION_PLAN,
            )
        )
        result.register_adapter(
            "fake-source-check",
            _SimpleActionAdapter(),
        )
        result.register_action_planner(
            DESIGN_SOURCE_CHECK_ACTION,
            lambda node: plan_design_action(
                selected_project,
                owner,
                node.config,
            ),
        )
        return result

    monkeypatch.setattr(project_runner_module, "_project_action_registry", registry)


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
    assert re.fullmatch(r"sha256-[0-9a-f]{64}", execution.plan_identity)
    assert execution.node_count == 1
    assert execution.graph == (("check", ()),)


def test_project_runner_compiles_target_inputs_and_retains_input_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, targets, recipe, implementation = _write_project(tmp_path)
    input_source = tmp_path / "ip/example/configs/variant.txt"
    input_source.write_text("paper\n", encoding="utf-8")
    recipe.write_text(
        _RECIPE.replace(
            'name = "smoke-recipe"',
            'name = "smoke-recipe"\n\n[inputs.variant]\nkind = "owner-path"',
        ).replace(
            'config = { target = "smoke", mode = "check" }',
            'config = { target = "smoke", mode = "check", '
            'variant = { input = "variant" } }',
        ),
        encoding="utf-8",
    )
    targets.write_text(
        _TARGETS.replace(
            "[targets.smoke]\ndescription = \"Offline smoke target\"",
            "[targets.smoke]\n"
            "description = \"Offline smoke target\"\n"
            "inputs = { variant = \"configs/variant.txt\" }",
        ),
        encoding="utf-8",
    )
    _install_simple_design_seam(monkeypatch, project, implementation)

    execution = ProjectRunner(project, "example").plan("smoke", "check")

    assert execution.record["inputs"] == {"variant": "configs/variant.txt"}
    assert execution.record["nodes"][0]["action_config"]["variant"] == (
        "configs/variant.txt"
    )
    assert {
        (source["scope"], source["path"])
        for source in execution.record["source_members"]
    } == {
        ("project", "ip/example/configs/targets.toml"),
        ("project", "ip/example/configs/smoke.toml"),
        ("project", "ip/example/configs/variant.txt"),
    }


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
    assert project.runs.read(
        owner="example",
        target="smoke",
        operation="all",
        run_id=run_id,
    )["status"] == "accepted"

    restored = execution.restore_result(run_id)
    assert restored.status == "accepted"
    assert restored.run_id == run_id
    assert set(restored.nodes) == {"check", "audit"}

    project.runs.clean(
        owner="example",
        target="smoke",
        operation="all",
        run_id=run_id,
    )
    assert not result.run_root.exists()
    with pytest.raises(RunStoreError):
        project.runs.read(
            owner="example",
            target="smoke",
            operation="all",
            run_id=run_id,
        )


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
