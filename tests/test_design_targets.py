from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import sys
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
    FlowExecutionError,
)
from sigilicon.flow.circuit_design import (
    DESIGN_ACTION_PLAN,
    DESIGN_SOURCE_CHECK_ACTION,
)
from sigilicon.workflows import design_flow
from sigilicon.workflows.design_flow import (
    DesignActionPlan,
    DesignTargetAdapter,
    plan_design_action,
)


@dataclass(frozen=True)
class _Owner:
    name: str
    root: Path


class _Project:
    """Small project seam for direct planner tests."""

    def __init__(self, root: Path, owner: str = "example") -> None:
        self.project_root = root.resolve()
        self._owner = _Owner(owner, (self.project_root / "ip" / owner).resolve())
        self.owners = (self._owner,)

    def owner(self, name: str) -> _Owner:
        if name != self._owner.name:
            raise ValueError(f"unknown owner: {name}")
        return self._owner

    def resolve_owner_file(
        self,
        owner: _Owner,
        value: object,
        field: str,
    ) -> tuple[Path, PurePosixPath]:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a path")
        relative = PurePosixPath(value)
        resolved = self.project_root.joinpath(*relative.parts).resolve()
        if not resolved.is_file() or not resolved.is_relative_to(owner.root):
            raise ValueError(f"{field} is outside the owner")
        return resolved, relative


def _project(tmp_path: Path) -> tuple[_Project, Path, Path]:
    root = tmp_path.resolve()
    source = root / "ip/example/dv/run.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import json\nprint(json.dumps({'passed': True}))\n",
        encoding="utf-8",
    )
    spec = root / "ip/example/dv/design.toml"
    spec.write_text("name = 'fixture'\n", encoding="utf-8")
    return _Project(root), source, spec


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
    plan = plan_design_action(project, "example", config)

    assert isinstance(plan, DesignActionPlan)
    assert plan.target == "leaf"
    assert plan.mode == "topology"
    assert plan.entrypoint == "ip/example/dv/run.py"
    assert tuple(member.path for member in plan.source_members) == (
        "ip/example/dv/run.py",
        "ip/example/dv/design.toml",
    )
    assert plan.source_members[0].record_text == source.read_text(encoding="utf-8")
    assert plan.source_members[1].record_text == spec.read_text(encoding="utf-8")
    assert plan.as_dict() == {
        "target": "leaf",
        "mode": "topology",
        "kind": "script",
        "entrypoint": "ip/example/dv/run.py",
        "spec_argument": "--spec",
        "spec": "ip/example/dv/design.toml",
        "default_args": ["--overwrite"],
        "evidence_role": "diagnostic",
        "evidence_level": "l0",
        "evidence_scope": "leaf-topology",
    }

    command = plan.bound_command(
        runner_path="/sealed/runner",
        spec_path="/sealed/spec",
    )
    assert command[:3] == (
        sys.executable,
        "-c",
        "from sigilicon.workflows.design_runner import main;main()",
    )
    assert command[3:9] == (
        "/sealed/runner",
        str(source.resolve()),
        "-",
        "/sealed/spec",
        str(spec.resolve()),
        "--spec",
    )
    assert command[9:] == (
        str(spec.resolve()),
        "--mode",
        "topology",
        "--overwrite",
    )


def test_plan_design_action_requires_direct_recipe_fields_and_safe_args(
    tmp_path: Path,
) -> None:
    project, source, _spec = _project(tmp_path)
    config = _config(source)

    with pytest.raises(ValueError, match="unknown configuration"):
        plan_design_action(project, "example", {**config, "description": "legacy"})
    with pytest.raises(ValueError, match="missing configuration"):
        plan_design_action(project, "example", {key: value for key, value in config.items() if key != "evidence_scope"})
    with pytest.raises(ValueError, match="cannot override routing"):
        plan_design_action(project, "example", {**config, "default_args": ["--mode=sync"]})
    with pytest.raises(ValueError, match="configured together"):
        plan_design_action(project, "example", {**config, "spec_argument": "--spec"})
    with pytest.raises(ValueError, match="canonical project-relative"):
        plan_design_action(project, "example", {**config, "entrypoint": "../dv/run.py"})


def test_plan_design_action_rejects_source_drift_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, source, _spec = _project(tmp_path)
    monkeypatch.setattr(design_flow, "source_member_matches", lambda _member: False)

    with pytest.raises(ValueError, match="source changed during planning"):
        plan_design_action(project, "example", _config(source))


def test_plan_design_action_accepts_owner_module_and_one_shared_module(
    tmp_path: Path,
) -> None:
    project, _source, _spec = _project(tmp_path)
    module = tmp_path / "ip/example/dv/transaction.py"
    module.write_text("print('{\"passed\": true}')\n", encoding="utf-8")
    owner_config = _config(module)
    owner_config.update(
        {
            "kind": "module",
            "entrypoint": "ip.example.dv.transaction",
        }
    )
    owner_plan = plan_design_action(project, "example", owner_config)
    assert owner_plan.kind == "module"
    assert owner_plan.source_members[0].location == module.resolve()
    assert owner_plan.source_members[0].scope == "project"

    shared_config = _config(module)
    shared_config.update(
        {
            "kind": "module",
            "entrypoint": "sigilicon.cli.design_lifecycle",
        }
    )
    shared_plan = plan_design_action(project, "example", shared_config)
    assert shared_plan.source_members[0].scope == "sigilicon-package"
    assert shared_plan.source_members[0].path == "sigilicon/cli/design_lifecycle.py"
    assert shared_plan.bound_command(
        runner_path="/sealed/runner",
        spec_path=None,
    )[5] == "sigilicon.cli"

    with pytest.raises(ValueError, match="project-owned module"):
        plan_design_action(
            project,
            "example",
            {**shared_config, "entrypoint": "sigilicon.cli.future_command"},
        )


def test_plan_design_action_rejects_symlinked_owner_source(tmp_path: Path) -> None:
    project, source, _spec = _project(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)

    with pytest.raises(ValueError, match="outside the owner"):
        plan_design_action(project, "example", _config(source))


def _adapter_context(tmp_path: Path) -> tuple[ActionContext, Path]:
    project, source, spec = _project(tmp_path)
    plan = plan_design_action(project, "example", _config(source, spec))
    action = ActionContract(
        kind=DESIGN_SOURCE_CHECK_ACTION,
        outputs=(ArtifactPort("evidence", "evidence.design-source-check"),),
        facts=(
            "passed",
            "execution-completed",
            "process-returncode",
            "evidence-role",
            "evidence-level",
            "evidence-scope",
            "product-qualification-conclusion",
        ),
        adapters=("project-design-source-check",),
        plan_input_kind=DESIGN_ACTION_PLAN,
    )
    action_plan = ActionPlan(
        DESIGN_ACTION_PLAN,
        plan,
        plan.as_dict(),
        plan.source_members,
    )
    config = ActionConfiguration(DESIGN_SOURCE_CHECK_ACTION, plan.as_dict())
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
            action_plan=action_plan,
            evidence=EvidenceEnvelope.from_action_config(plan.as_dict()),
        ),
        source,
    )


def test_design_adapter_consumes_typed_plan_and_exact_closure(tmp_path: Path) -> None:
    context, source = _adapter_context(tmp_path)

    result = DesignTargetAdapter().run(context)

    assert result.collected is not None
    assert result.collected.facts["passed"] is True
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
