from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sigilicon.flow import (
    ActionContract,
    AdapterSelection,
    ArtifactPort,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowContractError,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    SourceAssetsAdapter,
    load_source_assets,
)


def _git(owner_root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(owner_root), *args),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _commit_fixture(owner_root: Path) -> str:
    _git(owner_root, "init", "-q")
    _git(owner_root, "config", "user.email", "fixture@example.com")
    _git(owner_root, "config", "user.name", "Fixture")
    _git(owner_root, "add", ".")
    _git(owner_root, "commit", "-qm", "fixture")
    return _git(owner_root, "rev-parse", "HEAD")


def _write_assets(owner_root: Path, *, schema: int = 1) -> Path:
    assets = owner_root / "source-assets.toml"
    assets.write_text(
        f'''schema = {schema}
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "fixture-source"

[qualifiers]
variant = "variant_b"

[[artifacts]]
role = "rtl-sources"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["rtl/a.sv", "rtl/b.sv"]

[[artifacts]]
role = "constraints"
kind = "constraints.sdc"
materialization = "file"
members = ["constraints.sdc"]
''',
        encoding="utf-8",
    )
    return assets


def _fixture(owner_root: Path) -> tuple[FlowRegistry, FlowSpec, ExecutionProfile]:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="design.fixture-source",
            outputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("constraints", "constraints.sdc"),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    spec = FlowSpec(
        owner="fixture",
        flow_id="source-only",
        nodes=(
            FlowNode(
                "assets",
                "design.fixture-source",
                config={"source": "source-assets.toml"},
            ),
        ),
        targets=(FlowTarget("all", ("assets",)),),
        owner_root=owner_root,
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="source-only",
        selections=(AdapterSelection("design.fixture-source", "source-assets"),),
    )
    return registry, spec, profile


def test_source_assets_use_git_identity_and_materialize_a_run_snapshot(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    (owner_root / "rtl").mkdir(parents=True)
    (owner_root / "rtl/a.sv").write_text("module a; endmodule\n", encoding="utf-8")
    (owner_root / "rtl/b.sv").write_text("module b; endmodule\n", encoding="utf-8")
    (owner_root / "constraints.sdc").write_text("set_max_area 0\n", encoding="utf-8")
    _write_assets(owner_root)
    commit = _commit_fixture(owner_root)
    registry, spec, profile = _fixture(owner_root)
    engine = FlowEngine(registry)

    old_plan = engine.plan(spec, "all", profile)
    old_record = engine.plan_record(old_plan)
    source_record = old_record["nodes"][0]["source_assets"]
    assert source_record["git"] == {"commit": commit, "dirty": False}
    assert source_record["artifacts"]["rtl-sources"]["members"] == [
        "rtl/a.sv",
        "rtl/b.sv",
    ]
    (owner_root / "rtl/a.sv").write_text(
        "module a; logic changed; endmodule\n",
        encoding="utf-8",
    )
    stale = engine.preflight(old_plan, ExecutionEnvironment())
    assert stale.status == "blocked"
    assert next(
        check for check in stale.checks if check.requirement_kind == "git-source"
    ).status == "changed"
    with pytest.raises(FlowExecutionError, match="preflight"):
        engine.run(
            old_plan,
            artifact_root=tmp_path / "blocked-artifacts",
            run_id="a" * 32,
        )

    new_plan = engine.plan(spec, "all", profile)
    assert new_plan.planned_node("assets").source_assets.git.dirty is True
    result = engine.run(
        new_plan,
        artifact_root=tmp_path / "artifacts",
        run_id="b" * 32,
    )
    assert result.status == "accepted"
    source = result.nodes["assets"]
    assert source.artifacts["rtl-sources"].qualifiers["variant"] == "variant_b"
    manifest = json.loads(source.artifacts["rtl-sources"].path.read_text())
    assert manifest["members"][0] == {
        "path": "rtl/a.sv",
        "file": "files/rtl/a.sv",
    }
    snapshot = source.artifacts["rtl-sources"].path.parent / "files/rtl/a.sv"
    assert "logic changed" in snapshot.read_text(encoding="utf-8")
    assert source.artifacts["constraints"].path.read_text() == "set_max_area 0\n"
    for record in result.run_root.rglob("*.json"):
        assert str(tmp_path) not in record.read_text(encoding="utf-8")


def test_source_assets_loader_rejects_non_current_schema_and_owner_escape(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    assets = _write_assets(owner_root, schema=2)

    with pytest.raises(FlowContractError, match="current schema 1"):
        load_source_assets(
            assets,
            owner_root=owner_root,
            expected_owner="fixture",
        )

    assets.write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "escape"
qualifiers = {}

[[artifacts]]
role = "rtl-sources"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["../outside.sv"]
''',
        encoding="utf-8",
    )
    with pytest.raises(FlowContractError, match="owner root"):
        load_source_assets(
            assets,
            owner_root=owner_root,
            expected_owner="fixture",
        )
