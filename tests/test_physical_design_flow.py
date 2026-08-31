"""Tests for the minimal physical materialization interchange contract."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from physical_design_fixtures import routed_job, typed_result
from sigilicon.canonical import CanonicalSerializationError
from sigilicon.flow import (
    ActionBinding,
    ArtifactBinding,
    ExecutionEnvironment,
    FlowEngine,
    FlowNode,
    FlowSpec,
    FlowTarget,
)
from sigilicon.flow.physical_design import (
    MATERIALIZATION_PLAN_ADAPTER,
    PHYSICAL_DESIGN_RESULT_SOURCE_ACTION,
    PHYSICAL_DESIGN_SOURCE_ACTION,
    PHYSICAL_MATERIALIZATION_ACTION,
)
from sigilicon.layout.materialization import materialization_plan_from_json
from sigilicon.layout.physical_design import (
    physical_design_job_from_json,
    physical_design_job_id,
    physical_design_result_from_json,
    physical_design_result_id,
)
from sigilicon.workflows.action_registry import build_action_registry


def test_physical_design_serialization_is_reversible_and_strict() -> None:
    job = routed_job("serialization")
    result = typed_result(job)

    assert physical_design_job_from_json(job.canonical_json()) == job
    assert physical_design_result_from_json(result.canonical_json()) == result

    raw = json.loads(job.canonical_json())
    raw["unknown"] = True
    with pytest.raises(CanonicalSerializationError, match="unknown=.*unknown"):
        physical_design_job_from_json(json.dumps(raw))

    raw = json.loads(job.canonical_json())
    raw["technology"]["layers"][0]["kind"] = "unknown-layer-kind"
    with pytest.raises(CanonicalSerializationError, match="unknown LayerKind"):
        physical_design_job_from_json(json.dumps(raw))


def test_physical_design_job_identity_binds_complete_geometry_input() -> None:
    job = routed_job("identity")
    changed = replace(
        job,
        technology=replace(job.technology, manufacturing_grid_dbu=2),
    )

    identity = physical_design_job_id(job)
    assert identity != physical_design_job_id(changed)
    assert identity.startswith(
        "physical-design-job:identity-technology:identity:sha256:"
    )


def test_physical_result_identity_is_deterministic() -> None:
    job = routed_job("deterministic-result")
    first = typed_result(job, backend="external-contract-solver")
    second = typed_result(job, backend="external-contract-solver")

    assert first.canonical_json() == second.canonical_json()
    assert physical_design_result_id(first) == physical_design_result_id(second)


def test_source_artifacts_compile_an_executable_materialization_plan(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    job = routed_job("source-materialization")
    result = typed_result(job, backend="authored-fixture")
    (owner_root / "job.json").write_text(job.canonical_json(), encoding="utf-8")
    (owner_root / "result.json").write_text(
        result.canonical_json(), encoding="utf-8"
    )
    for name, role, kind in (
        ("job", "job", "physical-design.job"),
        ("result", "result", "physical-design.result"),
    ):
        (owner_root / f"{name}.toml").write_text(
            f'''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "physical-{name}"

[[artifacts]]
role = "{role}"
kind = "{kind}"
materialization = "file"
members = ["{name}.json"]
''',
            encoding="utf-8",
        )
    subprocess.run(("git", "init", "-q"), cwd=owner_root, check=True)
    subprocess.run(
        ("git", "config", "user.email", "fixture@example.com"),
        cwd=owner_root,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Fixture"), cwd=owner_root, check=True
    )
    subprocess.run(("git", "add", "."), cwd=owner_root, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=owner_root, check=True)

    spec = FlowSpec(
        owner="fixture",
        flow_id="physical-source-materialization",
        recipe_id="physical-source-materialization",
        nodes=(
            FlowNode(
                "job",
                PHYSICAL_DESIGN_SOURCE_ACTION,
                config={"source": "job.toml"},
            ),
            FlowNode(
                "result",
                PHYSICAL_DESIGN_RESULT_SOURCE_ACTION,
                config={"source": "result.toml"},
            ),
            FlowNode(
                "compile",
                PHYSICAL_MATERIALIZATION_ACTION,
                config={"target": {"owner": "fixture", "name": "layout"}},
                bindings=(
                    ArtifactBinding("job", "job", "job"),
                    ArtifactBinding("result", "result", "result"),
                ),
            ),
        ),
        targets=(FlowTarget("materialize", ("compile",)),),
        action_bindings=(
            ActionBinding(PHYSICAL_DESIGN_SOURCE_ACTION, "source-assets"),
            ActionBinding(PHYSICAL_DESIGN_RESULT_SOURCE_ACTION, "source-assets"),
            ActionBinding(
                PHYSICAL_MATERIALIZATION_ACTION,
                MATERIALIZATION_PLAN_ADAPTER,
            ),
        ),
        owner_root=owner_root,
    )
    engine = FlowEngine(build_action_registry())
    execution = engine.run(
        engine.plan(spec, "materialize"),
        artifact_root=tmp_path / "artifacts",
        environment=ExecutionEnvironment(),
        run_id="1" * 32,
    )
    plan = materialization_plan_from_json(
        execution.nodes["compile"].artifacts["plan"].path.read_text(
            encoding="utf-8"
        )
    )

    assert execution.status == "accepted"
    assert plan.executable
    assert plan.provenance.job_identity == physical_design_job_id(job)
    assert plan.provenance.result_identity == physical_design_result_id(result)
