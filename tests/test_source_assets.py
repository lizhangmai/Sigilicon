from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import stat
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
    SourceMember,
    load_source_assets,
    source_assets_payload,
)
from sigilicon.flow.source_assets import git_source


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
    (owner_root / "rtl/a.sv").chmod(0o755)
    (owner_root / "rtl/b.sv").write_text("module b; endmodule\n", encoding="utf-8")
    (owner_root / "constraints.sdc").write_text("set_max_area 0\n", encoding="utf-8")
    _write_assets(owner_root)
    commit = _commit_fixture(owner_root)
    registry, spec, profile = _fixture(owner_root)
    engine = FlowEngine(registry)

    old_plan = engine.plan(spec, "all", profile)
    old_record = engine.plan_record(old_plan)
    source_record = old_record["nodes"][0]["source_assets"]
    assert source_record["git"] == {"commit": commit, "changes": []}
    assert source_record["artifacts"]["rtl-sources"]["members"] == [
        {
            "path": "rtl/a.sv",
            "record_text": "module a; endmodule\n",
            "executable": True,
        },
        {
            "path": "rtl/b.sv",
            "record_text": "module b; endmodule\n",
            "executable": False,
        },
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
    dirty_record = engine.plan_record(new_plan)
    (owner_root / "rtl/a.sv").write_text(
        "module a; logic changed_again; endmodule\n",
        encoding="utf-8",
    )
    assert engine.preflight(new_plan, ExecutionEnvironment()).status == "blocked"
    replanned = engine.plan(spec, "all", profile)
    assert engine.plan_record(replanned) != dirty_record
    result = engine.run(
        replanned,
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
    assert "logic changed_again" in snapshot.read_text(encoding="utf-8")
    assert snapshot.stat().st_mode & stat.S_IXUSR
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


def test_source_assets_bind_git_and_members_to_one_source_root(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    foreign_root = tmp_path / "foreign"
    (owner_root / "rtl").mkdir(parents=True)
    (foreign_root / "rtl").mkdir(parents=True)
    for root in (owner_root, foreign_root):
        (root / "rtl/a.sv").write_text("module a; endmodule\n", encoding="utf-8")
        (root / "rtl/b.sv").write_text("module b; endmodule\n", encoding="utf-8")
    (owner_root / "constraints.sdc").write_text("set_max_area 0\n", encoding="utf-8")
    assets = _write_assets(owner_root)
    _commit_fixture(owner_root)
    source = load_source_assets(
        assets,
        owner_root=owner_root,
        expected_owner="fixture",
    )
    member = source.artifact("rtl-sources").members[0]

    with pytest.raises(FlowContractError, match="location disagrees"):
        replace(member, location=foreign_root / member.path)
    foreign_member = replace(
        member,
        source_root=foreign_root,
        location=foreign_root / member.path,
    )
    foreign_artifact = replace(
        source.artifact("rtl-sources"),
        members=(foreign_member,),
    )
    remaining = tuple(
        artifact
        for artifact in source.artifacts
        if artifact.role != foreign_artifact.role
    )
    with pytest.raises(FlowContractError, match="member root disagrees"):
        replace(source, artifacts=(foreign_artifact, *remaining))
    with pytest.raises(FlowContractError, match="Git source scope disagrees"):
        replace(source, owner_root=foreign_root)

    assert str(owner_root.resolve()) not in repr(source)
    assert str(owner_root.resolve()) not in repr(source.git)
    assert str(owner_root.resolve()) not in repr(member)
    assert str(owner_root.resolve()) not in json.dumps(source_assets_payload(source))


def test_git_source_identity_includes_its_checkout_scope(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    left = repository / "left"
    right = repository / "right"
    left.mkdir(parents=True)
    right.mkdir()
    (left / "source.txt").write_text("left\n", encoding="utf-8")
    (right / "source.txt").write_text("right\n", encoding="utf-8")
    _commit_fixture(repository)

    left_source = git_source(left)
    right_source = git_source(right)

    assert left_source.commit == right_source.commit
    assert left_source.changes == right_source.changes == ()
    assert left_source.repository_root == right_source.repository_root
    assert left_source.scope_root != right_source.scope_root
    assert left_source != right_source
