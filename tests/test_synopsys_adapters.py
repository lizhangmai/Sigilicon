from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from sigilicon.flow import (
    ActionBinding,
    ActionContract,
    ArtifactBinding,
    ArtifactPort,
    ExecutionEnvironment,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
)
from sigilicon.flow.standard_asic import register_standard_asic_actions
from sigilicon.flow.source_assets import SourceAssetsAdapter
from sigilicon.workflows.synopsys.dc import SynopsysDCAdapter


def _write_runner(owner_root: Path) -> None:
    runner = owner_root / "run-dc-fixture.py"
    runner.write_text(
        '''#!/usr/bin/env python3
import json
import os
from pathlib import Path

root = Path(os.environ["SIGILICON_DC_OUTPUT_ROOT"])
root.mkdir(parents=True, exist_ok=True)
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "variant_b"
rtl_sources = Path(os.environ["SIGILICON_DC_RTL_FILELIST"]).read_text(encoding="utf-8").splitlines()
assert len(rtl_sources) == 1
assert Path(rtl_sources[0]).is_file()
assert Path(os.environ["SIGILICON_DC_CONSTRAINTS"]).is_file()
assert Path(os.environ["SIGILICON_SYNOPSYS_DC_SHELL"]).is_file()
for role in ("RVT", "HVT", "LVT"):
    assert Path(os.environ[f"SIGILICON_STDCELL_{role}_DB"]).is_file()
for name in ("mapped.v", "mapped.sdc", "mapped.ddc"):
    (root / name).write_text(name + "\\n", encoding="utf-8")
for name in ("check_design.rpt", "timing.rpt"):
    (root / name).write_text("passed\\n", encoding="utf-8")
(root / "seen.json").write_text(
    json.dumps({"variant": os.environ["SIGILICON_DESIGN_VARIANT"]}),
    encoding="utf-8",
)
''',
        encoding="utf-8",
    )
    runner.chmod(0o755)


def _write_source_assets(owner_root: Path) -> None:
    (owner_root / "source-assets.toml").write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "dc-fixture"

[qualifiers]
variant = "variant_b"
corner = "nominal_b"

[[artifacts]]
role = "rtl-sources"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["rtl/top.sv"]

[[artifacts]]
role = "constraints"
kind = "constraints.sdc"
materialization = "file"
members = ["constraints.sdc"]

[[artifacts]]
role = "synthesis-recipe"
kind = "recipe.synthesis"
materialization = "manifest"
members = ["run-dc-fixture.py"]
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
        ("git", "config", "user.name", "Fixture"),
        cwd=owner_root,
        check=True,
    )
    subprocess.run(("git", "add", "."), cwd=owner_root, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=owner_root, check=True)


def test_synopsys_dc_adapter_manages_inputs_outputs_and_qualifiers(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    (owner_root / "rtl").mkdir()
    (owner_root / "rtl/top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    (owner_root / "constraints.sdc").write_text(
        "create_clock -period 1 clk\n", encoding="utf-8"
    )
    _write_runner(owner_root)
    _write_source_assets(owner_root)
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="design.fixture-assets",
            outputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("constraints", "constraints.sdc"),
                ArtifactPort("synthesis-recipe", "recipe.synthesis"),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    register_standard_asic_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    registry.register_adapter("synopsys-dc", SynopsysDCAdapter())
    spec = FlowSpec(
        owner="fixture",
        flow_id="dc-managed",
        recipe_id="dc-managed-recipe",
        nodes=(
            FlowNode(
                "assets",
                "design.fixture-assets",
                config={"source": "source-assets.toml"},
            ),
            FlowNode(
                "synthesis",
                "asic.synthesis",
                config={"runner": "run-dc-fixture.py"},
                bindings=(
                    ArtifactBinding("rtl-sources", "assets", "rtl-sources"),
                    ArtifactBinding("constraints", "assets", "constraints"),
                    ArtifactBinding(
                        "synthesis-recipe",
                        "assets",
                        "synthesis-recipe",
                    ),
                ),
            ),
        ),
        targets=(FlowTarget("synthesis", ("synthesis",)),),
        action_bindings=(
            ActionBinding("design.fixture-assets", "source-assets"),
            ActionBinding(
                "asic.synthesis",
                "synopsys-dc",
                config={
                    "timeout_seconds": 30,
                    "outputs": {
                        "mapped-netlist": "mapped.v",
                        "mapped-constraints": "mapped.sdc",
                        "checkpoint": "mapped.ddc",
                    },
                    "reports": ["check_design.rpt", "timing.rpt"],
                },
            ),
        ),
        owner_root=owner_root,
    )
    timing_root = tmp_path / "timing"
    timing_root.mkdir()
    timing_members = []
    for role in ("rvt", "hvt", "lvt"):
        library = timing_root / f"{role}.db"
        library.write_text(f"{role} timing fixture\n", encoding="utf-8")
        timing_members.append(
            ResolvedPlatformAssetMember(
                role=role,
                location=library,
            )
        )
    environment = ExecutionEnvironment(
        capabilities={
            "tool.synopsys-dc": ResolvedCapability(
                "dc-fixture",
                executable=owner_root / "run-dc-fixture.py",
            )
        },
        platform_assets=(
            ResolvedPlatformAsset(
                role="standard-cell-timing",
                kind="library.synopsys-db-set",
                identity="fixture-library",
                members=tuple(timing_members),
            ),
        ),
    )
    engine = FlowEngine(registry)
    result = engine.run(
        engine.plan(spec, "synthesis"),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="a" * 32,
    )

    assert result.status == "accepted", result.nodes["synthesis"].reason
    synthesis = result.nodes["synthesis"]
    assert synthesis.facts.as_mapping() == {}
    assert set(synthesis.artifacts) == {
        "checkpoint",
        "mapped-constraints",
        "mapped-netlist",
        "reports",
    }
    assert synthesis.artifacts["mapped-netlist"].qualifiers == {
        "corner": "nominal_b",
        "variant": "variant_b",
    }
    reports = json.loads(synthesis.artifacts["reports"].path.read_text())
    assert reports["kind"] == "report.collection"
    assert len(reports["members"]) == 2
    for record in result.run_root.rglob("*.json"):
        assert str(tmp_path) not in record.read_text(encoding="utf-8")


def test_synopsys_dc_adapter_rejects_environment_mapping_configuration() -> None:
    context = SimpleNamespace(
        adapter_config={
            "output_root_environment": "LEGACY_DC_ROOT",
            "input_environment": {
                "rtl-sources": "LEGACY_DC_RTL",
                "constraints": "LEGACY_DC_CONSTRAINTS",
            },
            "qualifier_environment": {"variant": "LEGACY_VARIANT"},
        }
    )

    with pytest.raises(FlowExecutionError, match="unknown configuration"):
        SynopsysDCAdapter()._configuration(context)
