from __future__ import annotations

import json
from pathlib import Path
import subprocess

from sigilicon.flow import (
    ActionContract,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowEngine,
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
from sigilicon.workflows.synopsys import SynopsysVCSAdapter


def _write_owner(owner_root: Path) -> None:
    (owner_root / "rtl").mkdir(parents=True)
    (owner_root / "dv").mkdir()
    (owner_root / "rtl/top.sv").write_text(
        "module top; endmodule\n",
        encoding="utf-8",
    )
    (owner_root / "dv/tb.sv").write_text(
        "module tb; endmodule\n",
        encoding="utf-8",
    )
    (owner_root / "mapped.v").write_text(
        "module top; endmodule\n",
        encoding="utf-8",
    )
    runner = owner_root / "run-vcs-fixture.py"
    runner.write_text(
        '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

target = sys.argv[1]
assert target in {"rtl", "structural", "gate"}
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "fixture_variant"
assert Path(os.environ["SIGILICON_SYNOPSYS_VCS"]).is_file()
assert Path(os.environ["SIGILICON_VCS_OUTPUT_ROOT"]).is_dir()
if target in {"rtl", "structural"}:
    rtl = Path(os.environ["SIGILICON_VCS_RTL_FILELIST"]).read_text().splitlines()
    assert len(rtl) == 1 and Path(rtl[0]).is_file()
if target in {"rtl", "gate"}:
    testbench = Path(os.environ["SIGILICON_VCS_TESTBENCH_FILELIST"]).read_text().splitlines()
    assert len(testbench) == 1 and Path(testbench[0]).is_file()
if target == "gate":
    assert Path(os.environ["SIGILICON_VCS_MAPPED_NETLIST"]).is_file()
if target in {"structural", "gate"}:
    for role in ("RVT", "HVT", "LVT"):
        assert Path(os.environ[f"SIGILICON_STDCELL_{role}_VERILOG"]).is_file()
print(json.dumps({"target": target, "passed": True}))
''',
        encoding="utf-8",
    )
    runner.chmod(0o755)
    (owner_root / "source-assets.toml").write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "vcs-fixture-source"

[qualifiers]
variant = "fixture_variant"
corner = "tt"

[[artifacts]]
role = "rtl-sources"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["rtl/top.sv"]

[[artifacts]]
role = "testbench"
kind = "source-set.systemverilog"
materialization = "manifest"
members = ["dv/tb.sv"]

[[artifacts]]
role = "simulation-recipe"
kind = "recipe.simulation"
materialization = "manifest"
members = ["run-vcs-fixture.py"]

[[artifacts]]
role = "mapped-netlist"
kind = "netlist.verilog"
materialization = "file"
members = ["mapped.v"]
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


def _registry(owner_root: Path) -> FlowRegistry:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="design.vcs-fixture",
            outputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("testbench", "source-set.systemverilog"),
                ArtifactPort("simulation-recipe", "recipe.simulation"),
                ArtifactPort("mapped-netlist", "netlist.verilog"),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    register_standard_asic_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    registry.register_adapter("synopsys-vcs", SynopsysVCSAdapter())
    return registry


def _flow(owner_root: Path) -> tuple[FlowSpec, ExecutionProfile]:
    recipe = ArtifactBinding(
        "simulation-recipe",
        "assets",
        "simulation-recipe",
    )
    spec = FlowSpec(
        owner="fixture",
        flow_id="vcs-managed",
        nodes=(
            FlowNode(
                "assets",
                "design.vcs-fixture",
                config={"source": "source-assets.toml"},
            ),
            FlowNode(
                "rtl",
                "asic.rtl-simulation",
                config={"runner": "run-vcs-fixture.py", "target": "rtl"},
                bindings=(
                    ArtifactBinding("rtl-sources", "assets", "rtl-sources"),
                    ArtifactBinding("testbench", "assets", "testbench"),
                    recipe,
                ),
            ),
            FlowNode(
                "structural",
                "asic.structural-elaboration",
                config={
                    "runner": "run-vcs-fixture.py",
                    "target": "structural",
                },
                bindings=(
                    ArtifactBinding("rtl-sources", "assets", "rtl-sources"),
                    recipe,
                ),
            ),
            FlowNode(
                "gate",
                "asic.gate-simulation",
                config={"runner": "run-vcs-fixture.py", "target": "gate"},
                bindings=(
                    ArtifactBinding("mapped-netlist", "assets", "mapped-netlist"),
                    ArtifactBinding("testbench", "assets", "testbench"),
                    recipe,
                ),
            ),
        ),
        targets=(
            FlowTarget("rtl", ("rtl",)),
            FlowTarget("structural", ("structural",)),
            FlowTarget("gate", ("gate",)),
        ),
        owner_root=owner_root,
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="vcs-fixture",
        selections=(
            AdapterSelection("design.vcs-fixture", "source-assets"),
            AdapterSelection(
                "asic.rtl-simulation",
                "synopsys-vcs",
                config={"timeout_seconds": 30},
            ),
            AdapterSelection(
                "asic.structural-elaboration",
                "synopsys-vcs",
                config={"timeout_seconds": 30},
            ),
            AdapterSelection(
                "asic.gate-simulation",
                "synopsys-vcs",
                config={"timeout_seconds": 30},
            ),
        ),
    )
    return spec, profile


def _environment(tmp_path: Path, executable: Path) -> ExecutionEnvironment:
    models: list[ResolvedPlatformAssetMember] = []
    for role in ("rvt", "hvt", "lvt"):
        model = tmp_path / f"models/{role}.v"
        model.parent.mkdir(exist_ok=True)
        model.write_text(f"module {role}_cell; endmodule\n", encoding="utf-8")
        models.append(
            ResolvedPlatformAssetMember(
                role=role,
                location=model,
            )
        )
    return ExecutionEnvironment(
        capabilities={
            "tool.synopsys-vcs": ResolvedCapability(
                "vcs-fixture",
                executable=executable,
            )
        },
        platform_assets=(
            ResolvedPlatformAsset(
                role="standard-cell-models",
                kind="library.verilog-model-set",
                identity="fixture-models",
                members=tuple(models),
            ),
        ),
    )


def test_synopsys_vcs_adapter_manages_rtl_structural_and_gate_inputs(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    engine = FlowEngine(_registry(owner_root))
    environment = _environment(tmp_path, owner_root / "run-vcs-fixture.py")

    for index, target in enumerate(("rtl", "structural", "gate"), start=1):
        result = engine.run(
            engine.plan(spec, target, profile),
            artifact_root=tmp_path / "artifacts",
            environment=environment,
            run_id=str(index) * 32,
        )
        outcome = result.nodes[target]
        evidence = json.loads(outcome.artifacts["evidence"].path.read_text())

        assert result.status == "accepted"
        assert outcome.facts["passed"] is True
        assert evidence["target"] == target
        assert evidence["qualifiers"] == {
            "corner": "tt",
            "variant": "fixture_variant",
        }
        assert str(tmp_path) not in json.dumps(evidence)
