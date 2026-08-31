from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.flow import ActionPlan, FlowExecutionError
from sigilicon.flow.layout import LAYOUT_ACTION_PLAN
from sigilicon.flow.source_assets import snapshot_source_member
from sigilicon.workflows import layout_flow
from sigilicon.workflows.layout_generation import LayoutPlanningResult

from conftest import write_component_owner, write_project_context


def _project_with_layout_spec(tmp_path: Path) -> tuple[Project, Path]:
    write_project_context(tmp_path)
    owner_root = tmp_path / "ip/example"
    owner_root.mkdir(parents=True, exist_ok=True)
    spec = owner_root / "leaf.toml"
    spec.write_text("# direct layout node fixture\n", encoding="utf-8")
    write_component_owner(tmp_path, "example", filesets={})
    return Project.from_project_root(tmp_path), spec.resolve()


def _retained_planning(project: Project, spec: Path) -> LayoutPlanningResult:
    """Build the smallest typed planning double without running a layout tool."""

    package_source = Path(layout_flow.__file__).resolve()
    planning = object.__new__(LayoutPlanningResult)
    object.__setattr__(
        planning,
        "spec",
        SimpleNamespace(
            path=spec,
            project=project,
            project_root=project.project_root,
            library="fixture_library",
            cell="fixture_cell",
            view="layout",
        ),
    )
    object.__setattr__(
        planning,
        "source_records",
        MappingProxyType(
            {
                spec: spec.read_text(encoding="utf-8"),
                package_source: package_source.read_text(encoding="utf-8"),
            }
        ),
    )
    object.__setattr__(
        planning,
        "plan",
        SimpleNamespace(
            canonical_json=lambda: '{"library":"fixture_library"}\n'
        ),
    )
    return planning


def _planned_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: dict[str, str],
) -> tuple[Project, Path, ActionPlan, layout_flow.LayoutInvocation]:
    project, spec = _project_with_layout_spec(tmp_path)
    planning = _retained_planning(project, spec)
    calls: list[tuple[Path, Project]] = []

    def fake_plan(path: Path, *, project: Project) -> LayoutPlanningResult:
        calls.append((path, project))
        return planning

    monkeypatch.setattr(layout_flow, "plan_layout_spec", fake_plan)
    action = layout_flow.plan_layout_action(
        project,
        project.owner("example"),
        config,
    )
    assert calls == [(spec, project)]
    return project, spec, action, action.require_value(layout_flow.LayoutInvocation)


def test_plan_layout_action_plans_a_direct_recipe_node_and_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, spec, action, invocation = _planned_action(
        tmp_path,
        monkeypatch,
        config={
            "target": "leaf",
            "operation": "generate",
            "spec": "ip/example/leaf.toml",
        },
    )

    assert invocation.target == "leaf"
    assert invocation.operation == "generate"
    assert invocation.spec == "ip/example/leaf.toml"
    assert invocation.planning.spec.project is project
    assert {member.scope for member in action.sources} == {
        "project",
        "sigilicon-package",
    }
    assert {
        member.location for member in action.sources
    } == set(invocation.planning.source_records)
    assert invocation.as_dict() == {
        "target": "leaf",
        "operation": "generate",
        "spec": "ip/example/leaf.toml",
        "layout": {"library": "fixture_library"},
    }
    assert spec in {member.location for member in action.sources}


def test_plan_layout_action_requires_verification_node_metadata(
    tmp_path: Path,
) -> None:
    _project_with_layout_spec(tmp_path)
    project = Project.from_project_root(tmp_path)
    with pytest.raises(ValueError, match="configuration fields"):
        layout_flow.plan_layout_action(
            project,
            project.owner("example"),
            {
                "target": "leaf",
                "operation": "verify-drc",
                "spec": "ip/example/leaf.toml",
                "check": "drc",
            },
        )

    with pytest.raises(ValueError, match="one of"):
        layout_flow.plan_layout_action(
            project,
            project.owner("example"),
            {
                "target": "leaf",
                "operation": "verify-all",
                "spec": "ip/example/leaf.toml",
            },
        )


def test_plan_layout_action_accepts_only_owner_relative_specs(
    tmp_path: Path,
) -> None:
    project, _ = _project_with_layout_spec(tmp_path)
    outside = tmp_path / "outside.toml"
    outside.write_text("outside\n", encoding="utf-8")

    with pytest.raises(ValueError, match="inside owner"):
        layout_flow.plan_layout_action(
            project,
            project.owner("example"),
            {
                "target": "leaf",
                "operation": "generate",
                "spec": "outside.toml",
            },
        )

    with pytest.raises(ValueError, match="canonical project-relative"):
        layout_flow.plan_layout_action(
            project,
            project.owner("example"),
            {
                "target": "leaf",
                "operation": "generate",
                "spec": "ip\\example\\leaf.toml",
            },
        )


def test_layout_action_plan_rejects_retained_source_record_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, spec, action, invocation = _planned_action(
        tmp_path,
        monkeypatch,
        config={
            "target": "leaf",
            "operation": "generate",
            "spec": "ip/example/leaf.toml",
        },
    )
    records = dict(invocation.planning.source_records)
    records[spec] = "different\n"
    object.__setattr__(invocation.planning, "source_records", records)

    with pytest.raises(FlowExecutionError, match="source closure"):
        layout_flow.LayoutActionAdapter._validate_source_closure(
            SimpleNamespace(action_plan=action),
            invocation,
        )


def test_layout_adapter_validates_direct_node_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, _spec, _action, invocation = _planned_action(
        tmp_path,
        monkeypatch,
        config={
            "target": "leaf",
            "operation": "generate",
            "spec": "ip/example/leaf.toml",
        },
    )
    context = SimpleNamespace(
        action_config={
            "target": "leaf",
            "operation": "generate",
            "spec": "ip/example/leaf.toml",
        }
    )

    layout_flow.LayoutActionAdapter._validate_action_config(context, invocation)

    context.action_config["operation"] = "verify-drc"
    with pytest.raises(FlowExecutionError, match="operation drift"):
        layout_flow.LayoutActionAdapter._validate_action_config(context, invocation)


def test_layout_adapter_validates_verification_evidence_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, _spec, _action, invocation = _planned_action(
        tmp_path,
        monkeypatch,
        config={
            "target": "leaf",
            "operation": "verify-drc",
            "spec": "ip/example/leaf.toml",
            "check": "drc",
            "evidence_role": "regression",
            "evidence_level": "l1",
            "evidence_scope": "fixture",
        },
    )
    context = SimpleNamespace(
        action_config={
            "target": "leaf",
            "operation": "verify-drc",
            "spec": "ip/example/leaf.toml",
            "check": "drc",
            "evidence_role": "regression",
            "evidence_level": "l1",
            "evidence_scope": "fixture",
        },
        require_evidence=lambda: SimpleNamespace(
            role="regression",
            level="l1",
            scope="fixture",
        ),
    )

    layout_flow.LayoutActionAdapter._validate_action_config(context, invocation)

    context.action_config["evidence_scope"] = "different"
    with pytest.raises(FlowExecutionError, match="evidence envelope"):
        layout_flow.LayoutActionAdapter._validate_action_config(context, invocation)


def test_layout_adapter_requires_the_exact_common_action_plan_source_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, spec, action, invocation = _planned_action(
        tmp_path,
        monkeypatch,
        config={
            "target": "leaf",
            "operation": "generate",
            "spec": "ip/example/leaf.toml",
        },
    )
    recipe_source = project.project_root / "ip/example/recipe.toml"
    recipe_source.write_text("recipe = true\n", encoding="utf-8")
    extra = snapshot_source_member(
        recipe_source,
        source_root=project.project_root,
        record_text=recipe_source.read_text(encoding="utf-8"),
        source_label="layout recipe",
    )
    context = SimpleNamespace(
        action_plan=ActionPlan(
            LAYOUT_ACTION_PLAN,
            invocation,
            invocation.as_dict(),
            (*action.sources, extra),
        )
    )

    with pytest.raises(FlowExecutionError, match="source closure"):
        layout_flow.LayoutActionAdapter._validate_source_closure(
            context,
            invocation,
        )

    spec.write_text("changed\n", encoding="utf-8")
    context.action_plan = action
    with pytest.raises(FlowExecutionError, match="source changed"):
        layout_flow.LayoutActionAdapter._validate_source_closure(
            context,
            invocation,
        )
