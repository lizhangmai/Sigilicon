from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from conftest import StagedAdapterFixture
from sigilicon.flow import (
    ActionBinding,
    ActionContext,
    ActionContract,
    AdapterExecution,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
)
from sigilicon.flow.standard_asic import register_standard_asic_actions
from sigilicon.workflows.synopsys.dc import SynopsysDCAdapter


class SourceAssetsAdapter(StagedAdapterFixture):
    def __init__(self, owner_root: Path) -> None:
        self.owner_root = owner_root

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        rtl_manifest = context.output_path("rtl-sources", "rtl-sources.json")
        rtl_snapshot = rtl_manifest.parent / "files/rtl/top.sv"
        rtl_snapshot.parent.mkdir(parents=True)
        shutil.copy2(self.owner_root / "rtl/top.sv", rtl_snapshot)
        rtl_manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "source-set-manifest",
                    "kind": "source-set.systemverilog",
                    "qualifiers": {
                        "variant": "variant_b",
                        "corner": "nominal_b",
                    },
                    "members": [
                        {
                            "path": "rtl/top.sv",
                            "file": "files/rtl/top.sv",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        context.output_path("constraints", "constraints.sdc").write_text(
            "create_clock -period 1 clk\n",
            encoding="utf-8",
        )
        recipe_manifest = context.output_path("synthesis-recipe", "recipe.json")
        recipe_snapshot = recipe_manifest.parent / "files/run-dc-fixture.py"
        recipe_snapshot.parent.mkdir(parents=True)
        shutil.copy2(self.owner_root / "run-dc-fixture.py", recipe_snapshot)
        recipe_manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "source-set-manifest",
                    "kind": "recipe.synthesis",
                    "qualifiers": {
                        "variant": "variant_b",
                        "corner": "nominal_b",
                    },
                    "members": [
                        {
                            "path": "run-dc-fixture.py",
                            "file": "files/run-dc-fixture.py",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return AdapterExecution.succeeded()

    def collect_result(
        self,
        context: ActionContext,
        execution: AdapterExecution,
    ) -> CollectedActionResult:
        qualifiers = {"variant": "variant_b", "corner": "nominal_b"}
        return CollectedActionResult(
            artifacts=(
                ProducedArtifact(
                    "rtl-sources",
                    "source-set.systemverilog",
                    context.output_path("rtl-sources", "rtl-sources.json"),
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "constraints",
                    "constraints.sdc",
                    context.output_path("constraints", "constraints.sdc"),
                    qualifiers=qualifiers,
                ),
                ProducedArtifact(
                    "synthesis-recipe",
                    "recipe.synthesis",
                    context.output_path("synthesis-recipe", "recipe.json"),
                    qualifiers=qualifiers,
                ),
            )
        )


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


def test_synopsys_dc_adapter_manages_inputs_outputs_and_qualifiers(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    (owner_root / "rtl").mkdir()
    (owner_root / "rtl/top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    _write_runner(owner_root)
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="design.fixture-assets",
            outputs=(
                ArtifactPort("rtl-sources", "source-set.systemverilog"),
                ArtifactPort("constraints", "constraints.sdc"),
                ArtifactPort("synthesis-recipe", "recipe.synthesis"),
            ),
            adapters=("fixture-assets",),
        )
    )
    register_standard_asic_actions(registry)
    registry.register_adapter("fixture-assets", SourceAssetsAdapter(owner_root))
    registry.register_adapter("synopsys-dc", SynopsysDCAdapter())
    spec = FlowSpec(
        owner="fixture",
        flow_id="dc-managed",
        recipe_id="dc-managed-recipe",
        nodes=(
            FlowNode("assets", "design.fixture-assets"),
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
            ActionBinding("design.fixture-assets", "fixture-assets"),
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
    assert synthesis.facts["passed"] is True
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
