from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sigilicon.flow import (
    ActionConfiguration,
    ActionContext,
    ActionPlan,
    ActionContract,
    AdapterConfiguration,
    ArtifactPort,
    EvidenceEnvelope,
    FactKind,
    FactSchema,
    FactSource,
    FactSpec,
    FlowExecutionError,
)
from sigilicon.flow.circuit_design import (
    DESIGN_ACTION_PLAN,
    DESIGN_SOURCE_CHECK_ACTION,
)
from sigilicon.domain.repository import Project
from sigilicon.workflows import design_flow
from sigilicon.workflows.design_flow import (
    DesignInvocation,
    DesignTargetAdapter,
    plan_design_action,
)

from conftest import write_component_owner, write_project_context

def _project(tmp_path: Path) -> tuple[Project, Path, Path]:
    root = tmp_path.resolve()
    write_project_context(root)
    write_component_owner(root, "example", filesets={})
    source = root / "ip/example/dv/run.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import json\nprint(json.dumps({'passed': True}))\n",
        encoding="utf-8",
    )
    spec = root / "ip/example/dv/design.toml"
    spec.write_text("name = 'fixture'\n", encoding="utf-8")
    return Project.from_project_root(root), source, spec


def _config(source: Path, spec: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target": "leaf",
        "mode": "topology",
        "kind": "script",
        "entrypoint": source.relative_to(source.parents[3]).as_posix(),
        "evidence_role": "diagnostic",
        "evidence_level": "l0",
        "evidence_scope": "leaf-topology",
    }
    if spec is not None:
        project_root = source.parents[3]
        result.update(
            {
                "spec_argument": "--spec",
                "spec": spec.relative_to(project_root).as_posix(),
            }
        )
    return result


def test_plan_design_action_snapshots_runner_spec_and_binds_command(
    tmp_path: Path,
) -> None:
    project, source, spec = _project(tmp_path)

    config = _config(source, spec)
    config["default_args"] = ["--overwrite"]
    plan = plan_design_action(project, project.owner("example"), config)
    invocation = plan.require_value(DesignInvocation)

    assert isinstance(plan, ActionPlan)
    assert invocation.target == "leaf"
    assert invocation.mode == "topology"
    assert invocation.entrypoint == "ip/example/dv/run.py"
    assert tuple(member.path for member in plan.sources) == (
        "ip/example/dv/run.py",
        "ip/example/dv/design.toml",
    )
    assert plan.sources[0].record_text == source.read_text(encoding="utf-8")
    assert plan.sources[1].record_text == spec.read_text(encoding="utf-8")
    assert plan.record == {
        "target": "leaf",
        "mode": "topology",
        "kind": "script",
        "entrypoint": "ip/example/dv/run.py",
        "spec_argument": "--spec",
        "spec": "ip/example/dv/design.toml",
        "default_args": ("--overwrite",),
        "evidence_role": "diagnostic",
        "evidence_level": "l0",
        "evidence_scope": "leaf-topology",
    }

def test_plan_design_action_requires_direct_recipe_fields_and_safe_args(
    tmp_path: Path,
) -> None:
    project, source, _spec = _project(tmp_path)
    config = _config(source)

    with pytest.raises(ValueError, match="unknown configuration"):
        plan_design_action(project, project.owner("example"), {**config, "description": "legacy"})
    with pytest.raises(ValueError, match="missing configuration"):
        plan_design_action(project, project.owner("example"), {key: value for key, value in config.items() if key != "evidence_scope"})
    with pytest.raises(ValueError, match="cannot override routing"):
        plan_design_action(project, project.owner("example"), {**config, "default_args": ["--mode=sync"]})
    with pytest.raises(ValueError, match="configured together"):
        plan_design_action(project, project.owner("example"), {**config, "spec_argument": "--spec"})
    with pytest.raises(ValueError, match="canonical project-relative"):
        plan_design_action(project, project.owner("example"), {**config, "entrypoint": "../dv/run.py"})


def test_plan_design_action_rejects_source_drift_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, source, _spec = _project(tmp_path)
    monkeypatch.setattr(design_flow, "source_member_matches", lambda _member: False)

    with pytest.raises(ValueError, match="source changed during planning"):
        plan_design_action(project, project.owner("example"), _config(source))


def test_plan_design_action_rejects_symlinked_owner_source(tmp_path: Path) -> None:
    project, source, _spec = _project(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)

    with pytest.raises(ValueError, match="inside owner"):
        plan_design_action(project, project.owner("example"), _config(source))


def _adapter_context(tmp_path: Path) -> tuple[ActionContext, Path]:
    project, source, spec = _project(tmp_path)
    plan = plan_design_action(project, project.owner("example"), _config(source, spec))
    invocation = plan.require_value(DesignInvocation)
    action = ActionContract(
        kind=DESIGN_SOURCE_CHECK_ACTION,
        outputs=(ArtifactPort("evidence", "evidence.design-source-check"),),
        fact_schema=FactSchema(
            DESIGN_SOURCE_CHECK_ACTION,
            (
                FactSpec("passed", FactKind.BOOLEAN),
                FactSpec("process-returncode", FactKind.INTEGER),
                FactSpec(
                    "evidence-role",
                    FactKind.TEXT,
                    enum_values=(
                        "diagnostic",
                        "regression",
                        "qualification",
                        "signoff",
                    ),
                ),
                FactSpec(
                    "evidence-level",
                    FactKind.TEXT,
                    enum_values=("l0", "l1", "l2", "l3", "l4"),
                ),
                FactSpec("evidence-scope", FactKind.TEXT),
                FactSpec("product-qualification-conclusion", FactKind.BOOLEAN),
            ),
        ),
        adapters=("project-design-source-check",),
        plan_input_kind=DESIGN_ACTION_PLAN,
    )
    config = ActionConfiguration(DESIGN_SOURCE_CHECK_ACTION, invocation.as_dict())
    return (
        ActionContext(
            node_id="leaf-topology",
            action=action,
            run_root=tmp_path / "run",
            work_root=tmp_path / "run/work",
            output_root=tmp_path / "run/output",
            log_root=tmp_path / "run/logs",
            inputs={},
            action_config=config,
            adapter_config=AdapterConfiguration(
                "project-design-source-check",
                {"timeout_seconds": 30},
            ),
            capabilities={},
            platform_assets={},
            action_plan=plan,
            evidence=EvidenceEnvelope.from_action_config(invocation.as_dict()),
            project_scope=project.scope(project.owner("example")),
        ),
        source,
    )


def test_design_adapter_consumes_typed_plan_and_exact_closure(tmp_path: Path) -> None:
    context, source = _adapter_context(tmp_path)

    result = DesignTargetAdapter().run(context)

    assert result.collected is not None
    facts = result.collected.facts
    assert facts.schema == context.action.fact_schema
    assert facts.source == FactSource(DESIGN_SOURCE_CHECK_ACTION, "leaf-topology")
    assert facts["passed"] is True
    evidence = (
        context.output_root / "evidence" / "design-evidence.json"
    ).read_text(encoding="utf-8")
    assert '"contract_kind": "design-action-evidence"' in evidence
    assert source.read_text(encoding="utf-8") in (context.action_plan.sources[0].record_text if context.action_plan else "")


def test_design_adapter_rejects_plan_or_config_drift(tmp_path: Path) -> None:
    context, _source = _adapter_context(tmp_path)
    assert context.action_plan is not None
    drifted = dict(context.action_config.values)
    drifted["mode"] = "analysis"
    object.__setattr__(
        context,
        "action_config",
        ActionConfiguration(DESIGN_SOURCE_CHECK_ACTION, drifted),
    )

    with pytest.raises(FlowExecutionError, match="configuration drift"):
        DesignTargetAdapter().run(context)
