from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from sigilicon.flow import (
    ActionContext,
    ActionContract,
    AdapterExecution,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    CollectedActionResult,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowEngine,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ProducedArtifact,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    SynopsysDCAdapter,
    register_standard_asic_actions,
)


class SourceAssetsAdapter:
    version = "1"

    def __init__(self, owner_root: Path) -> None:
        self.owner_root = owner_root

    def validate_inputs(self, context: ActionContext) -> tuple[str, ...]:
        return ()

    def prepare(self, context: ActionContext) -> None:
        pass

    def execute(self, context: ActionContext) -> AdapterExecution:
        context.output_path("rtl-sources", "rtl-sources.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "source-set-manifest",
                    "kind": "source-set.systemverilog",
                    "qualifiers": {
                        "variant": "product_0p9v",
                        "corner": "tt0p9v25c",
                    },
                    "fingerprint": "0" * 64,
                    "members": [
                        {
                            "path": "rtl/top.sv",
                            "digest": sha256(
                                (self.owner_root / "rtl/top.sv").read_bytes()
                            ).hexdigest(),
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
        context.output_path("synthesis-recipe", "recipe.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contract_kind": "source-set-manifest",
                    "kind": "recipe.synthesis",
                    "qualifiers": {
                        "variant": "product_0p9v",
                        "corner": "tt0p9v25c",
                    },
                    "fingerprint": "1" * 64,
                    "members": [
                        {
                            "path": "run-dc-fixture.py",
                            "digest": sha256(
                                (self.owner_root / "run-dc-fixture.py").read_bytes()
                            ).hexdigest(),
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
        qualifiers = {"variant": "product_0p9v", "corner": "tt0p9v25c"}
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

root = Path(os.environ["MANAGED_DC_ROOT"])
root.mkdir(parents=True, exist_ok=True)
assert os.environ["DESIGN_VARIANT"] == "product_0p9v"
rtl_sources = Path(os.environ["RTL_SOURCE_SET"]).read_text(encoding="utf-8").splitlines()
assert len(rtl_sources) == 1
assert Path(rtl_sources[0]).is_file()
assert Path(os.environ["DESIGN_CONSTRAINTS"]).is_file()
assert Path(os.environ["SIGILICON_SYNOPSYS_DC_SHELL"]).is_file()
for role in ("RVT", "HVT", "LVT"):
    assert Path(os.environ[f"SIGILICON_STDCELL_{role}_DB"]).is_file()
for name in ("mapped.v", "mapped.sdc", "mapped.ddc"):
    (root / name).write_text(name + "\\n", encoding="utf-8")
for name in ("check_design.rpt", "timing.rpt"):
    (root / name).write_text("passed\\n", encoding="utf-8")
(root / "seen.json").write_text(
    json.dumps({"variant": os.environ["DESIGN_VARIANT"]}),
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
    registry.register_adapter("synopsys-dc", SynopsysDCAdapter(owner_root))
    spec = FlowSpec(
        owner="fixture",
        flow_id="dc-managed",
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
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="dc-fixture",
        selections=(
            AdapterSelection("design.fixture-assets", "fixture-assets"),
            AdapterSelection(
                "asic.synthesis",
                "synopsys-dc",
                config={
                    "output_root_environment": "MANAGED_DC_ROOT",
                    "timeout_seconds": 30,
                    "input_environment": {
                        "rtl-sources": "RTL_SOURCE_SET",
                        "constraints": "DESIGN_CONSTRAINTS",
                    },
                    "qualifier_environment": {"variant": "DESIGN_VARIANT"},
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
                digest=sha256(library.read_bytes()).hexdigest(),
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
                digest="0" * 64,
                members=tuple(timing_members),
            ),
        ),
    )
    engine = FlowEngine(registry)
    result = engine.run(
        engine.plan(spec, "synthesis", profile),
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
        "corner": "tt0p9v25c",
        "variant": "product_0p9v",
    }
    reports = json.loads(synthesis.artifacts["reports"].path.read_text())
    assert reports["kind"] == "report.collection"
    assert len(reports["members"]) == 2
    for record in result.run_root.rglob("*.json"):
        assert str(tmp_path) not in record.read_text(encoding="utf-8")
