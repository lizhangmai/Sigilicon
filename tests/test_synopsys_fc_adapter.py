from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionContract,
    AdapterSelection,
    ArtifactBinding,
    ArtifactPort,
    ExecutionEnvironment,
    ExecutionProfile,
    FlowEngine,
    FlowExecutionError,
    FlowNode,
    FlowRegistry,
    FlowSpec,
    FlowTarget,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    SourceAssetsAdapter,
    SynopsysFCAdapter,
    register_standard_asic_actions,
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _write_owner(owner_root: Path, *, fail_pnr: bool = False) -> None:
    owner_root.mkdir()
    (owner_root / "mapped.v").write_text("module top; endmodule\n", encoding="utf-8")
    (owner_root / "mapped.sdc").write_text(
        "create_clock -period 1 clk\n",
        encoding="utf-8",
    )
    (owner_root / "build-reference.tcl").write_text(
        "# reference fixture\n",
        encoding="utf-8",
    )
    (owner_root / "place-route.tcl").write_text(
        "# implementation fixture\n",
        encoding="utf-8",
    )
    (owner_root / "site.lef").write_text("END LIBRARY\n", encoding="utf-8")
    failure = "raise SystemExit(7)" if fail_pnr else "pass"
    _write_executable(
        owner_root / "run-fc-fixture.py",
        f'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys

target = sys.argv[1]
assert target in {{"library", "pnr"}}
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "fixture_variant"
assert os.environ["SIGILICON_DESIGN_CORNER"] == "tt"
assert os.environ["SIGILICON_DESIGN_TOP"] == "top"
assert Path(os.environ["SIGILICON_FC_WORK_ROOT"]).is_dir()

if target == "library":
    assert Path(os.environ["SIGILICON_SYNOPSYS_LM_SHELL"]).is_file()
    for name in (
        "SIGILICON_FC_TECH_FILE",
        "SIGILICON_FC_TECH_LEF",
        "SIGILICON_STDCELL_RVT_LEF",
        "SIGILICON_STDCELL_HVT_LEF",
        "SIGILICON_STDCELL_LVT_LEF",
        "SIGILICON_STDCELL_RVT_DB",
        "SIGILICON_STDCELL_HVT_DB",
        "SIGILICON_STDCELL_LVT_DB",
    ):
        assert Path(os.environ[name]).is_file()
    reference = Path(os.environ["SIGILICON_FC_REFERENCE_NDM"])
    reference.mkdir(parents=True)
    (reference / "library.ndm").write_text("reference library\\n")
    report = Path(os.environ["SIGILICON_FC_LIBRARY_CHECK_REPORT"])
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("library check passed\\n")
else:
    {failure}
    assert Path(os.environ["SIGILICON_SYNOPSYS_FC_SHELL"]).is_file()
    assert Path(os.environ["SIGILICON_FC_MAPPED_NETLIST"]).read_text().startswith("module top")
    assert "create_clock" in Path(os.environ["SIGILICON_FC_MAPPED_SDC"]).read_text()
    reference = Path(os.environ["SIGILICON_FC_REFERENCE_NDM"])
    assert (reference / "library.ndm").read_text() == "reference library\\n"
    assert Path(os.environ["SIGILICON_FC_TLUPLUS"]).is_file()
    assert Path(os.environ["SIGILICON_FC_GDS_MAP"]).is_file()
    files = {{
        "SIGILICON_FC_ROUTED_NETLIST": "module top; endmodule\\n",
        "SIGILICON_FC_ROUTED_CONSTRAINTS": "create_clock -period 1 clk\\n",
        "SIGILICON_FC_GDS": "gds fixture\\n",
        "SIGILICON_FC_DESIGN_CHECK_REPORT": "design passed\\n",
        "SIGILICON_FC_STRUCTURAL_REPORT": "structure passed\\n",
        "SIGILICON_FC_QOR_REPORT": "qor passed\\n",
        "SIGILICON_FC_TIMING_REPORT": "timing passed\\n",
        "SIGILICON_FC_AREA_REPORT": "area passed\\n",
        "SIGILICON_FC_POWER_REPORT": "power passed\\n",
        "SIGILICON_FC_DRC_REPORT": "drc passed\\n",
    }}
    for name, content in files.items():
        path = Path(os.environ[name])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    checkpoint = Path(os.environ["SIGILICON_FC_CHECKPOINT"])
    checkpoint.mkdir(parents=True)
    (checkpoint / "top.ndm").write_text("routed checkpoint\\n")
''',
    )
    (owner_root / "source-revision.toml").write_text(
        '''schema = 1
contract_kind = "source-asset-revision"
path_scope = "owner"
owner = "fixture"
name = "fc-fixture-source"

[qualifiers]
variant = "fixture_variant"
corner = "tt"

[[artifacts]]
role = "mapped-netlist"
kind = "netlist.verilog"
materialization = "file"
members = ["mapped.v"]

[[artifacts]]
role = "mapped-constraints"
kind = "constraints.sdc"
materialization = "file"
members = ["mapped.sdc"]

[[artifacts]]
role = "reference-library-recipe"
kind = "recipe.reference-library"
materialization = "manifest"
members = ["run-fc-fixture.py", "build-reference.tcl", "site.lef"]

[[artifacts]]
role = "implementation-recipe"
kind = "recipe.physical-implementation"
materialization = "manifest"
members = ["run-fc-fixture.py", "place-route.tcl"]
''',
        encoding="utf-8",
    )


def _registry(owner_root: Path) -> FlowRegistry:
    registry = FlowRegistry()
    registry.register_action(
        ActionContract(
            kind="design.fc-fixture",
            outputs=(
                ArtifactPort("mapped-netlist", "netlist.verilog"),
                ArtifactPort("mapped-constraints", "constraints.sdc"),
                ArtifactPort(
                    "reference-library-recipe",
                    "recipe.reference-library",
                ),
                ArtifactPort(
                    "implementation-recipe",
                    "recipe.physical-implementation",
                ),
            ),
            adapters=("source-assets",),
            resolves_source_revision=True,
        )
    )
    register_standard_asic_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    registry.register_adapter("synopsys-fc", SynopsysFCAdapter(owner_root))
    return registry


def _flow(owner_root: Path) -> tuple[FlowSpec, ExecutionProfile]:
    spec = FlowSpec(
        owner="fixture",
        flow_id="fc-managed",
        nodes=(
            FlowNode(
                "assets",
                "design.fc-fixture",
                config={"revision": "source-revision.toml"},
            ),
            FlowNode(
                "reference-library",
                "asic.reference-library-construction",
                config={
                    "runner": "run-fc-fixture.py",
                    "target": "library",
                    "top": "top",
                },
                bindings=(
                    ArtifactBinding(
                        "reference-library-recipe",
                        "assets",
                        "reference-library-recipe",
                    ),
                ),
            ),
            FlowNode(
                "implementation",
                "asic.physical-implementation",
                config={
                    "runner": "run-fc-fixture.py",
                    "target": "pnr",
                    "top": "top",
                },
                bindings=(
                    ArtifactBinding("mapped-netlist", "assets", "mapped-netlist"),
                    ArtifactBinding(
                        "mapped-constraints",
                        "assets",
                        "mapped-constraints",
                    ),
                    ArtifactBinding(
                        "implementation-recipe",
                        "assets",
                        "implementation-recipe",
                    ),
                    ArtifactBinding(
                        "reference-library",
                        "reference-library",
                        "reference-library",
                    ),
                ),
            ),
        ),
        targets=(
            FlowTarget("reference-library", ("reference-library",)),
            FlowTarget("implementation", ("implementation",)),
        ),
        owner_root=owner_root,
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="fc-fixture",
        selections=(
            AdapterSelection("design.fc-fixture", "source-assets"),
            AdapterSelection(
                "asic.reference-library-construction",
                "synopsys-fc",
                config={
                    "timeout_seconds": 30,
                    "outputs": {
                        "reference-library": "reference.ndm",
                        "library-check-report": "check_workspace.rpt",
                    },
                },
            ),
            AdapterSelection(
                "asic.physical-implementation",
                "synopsys-fc",
                config={
                    "timeout_seconds": 30,
                    "outputs": {
                        "routed-netlist": "routed.v",
                        "routed-constraints": "routed.sdc",
                        "layout-stream": "routed.gds",
                        "checkpoint": "routed.ndm",
                        "design-check-report": "check_design.rpt",
                        "structural-report": "protected_inventory.tsv",
                        "qor-report": "qor.rpt",
                        "timing-report": "timing.rpt",
                        "area-report": "area.rpt",
                        "power-report": "power.rpt",
                        "drc-report": "drc.rpt",
                    },
                },
            ),
        ),
    )
    return spec, profile


def _member(path: Path, role: str) -> ResolvedPlatformAssetMember:
    return ResolvedPlatformAssetMember(
        role=role,
        digest=sha256(path.read_bytes()).hexdigest(),
        location=path,
    )


def _environment(tmp_path: Path) -> tuple[ExecutionEnvironment, dict[str, Path]]:
    collateral: dict[str, Path] = {}
    for name in (
        "technology-file",
        "technology-lef",
        "tluplus",
        "gds-layer-map",
    ):
        path = tmp_path / "platform" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"{name} fixture\n", encoding="utf-8")
        collateral[name] = path
    physical: list[ResolvedPlatformAssetMember] = []
    timing: list[ResolvedPlatformAssetMember] = []
    for role in ("rvt", "hvt", "lvt"):
        lef = tmp_path / "lef" / f"{role}.lef"
        lef.parent.mkdir(exist_ok=True)
        lef.write_text(f"{role} LEF fixture\n", encoding="utf-8")
        physical.append(_member(lef, role))
        db = tmp_path / "db" / f"{role}.db"
        db.parent.mkdir(exist_ok=True)
        db.write_text(f"{role} DB fixture\n", encoding="utf-8")
        timing.append(_member(db, role))
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    lm_shell = bin_root / "lm_shell"
    fc_shell = bin_root / "fc_shell"
    for executable in (lm_shell, fc_shell):
        _write_executable(executable, "#!/bin/sh\nexit 0\n")
    environment = ExecutionEnvironment(
        capabilities={
            "tool.synopsys-library-manager": ResolvedCapability(
                "library-manager@fixture",
                executable=lm_shell,
            ),
            "tool.synopsys-fc": ResolvedCapability(
                "fusion-compiler@fixture",
                executable=fc_shell,
            ),
        },
        platform_assets=(
            ResolvedPlatformAsset(
                role="physical-technology",
                kind="platform.physical-view-set",
                identity="physical-technology@fixture",
                digest="1" * 64,
                members=tuple(_member(collateral[role], role) for role in collateral),
            ),
            ResolvedPlatformAsset(
                role="standard-cell-physical",
                kind="library.lef-set",
                identity="standard-cell-lef@fixture",
                digest="2" * 64,
                members=tuple(physical),
            ),
            ResolvedPlatformAsset(
                role="standard-cell-timing",
                kind="library.synopsys-db-set",
                identity="standard-cell-db@fixture",
                digest="3" * 64,
                members=tuple(timing),
            ),
        ),
    )
    return environment, collateral


def test_synopsys_fc_adapter_runs_separate_library_and_pnr_actions(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))
    plan = engine.plan(spec, "implementation", profile)

    assert plan.topology == ("assets", "reference-library", "implementation")
    result = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="a" * 32,
    )

    assert result.status == "accepted"
    reference = result.nodes["reference-library"]
    implementation = result.nodes["implementation"]
    assert set(reference.artifacts) == {
        "execution-evidence",
        "library-check-report",
        "reference-library",
    }
    assert set(implementation.artifacts) == {
        "area-report",
        "checkpoint",
        "design-check-report",
        "drc-report",
        "execution-evidence",
        "layout-stream",
        "power-report",
        "qor-report",
        "routed-constraints",
        "routed-netlist",
        "timing-report",
        "structural-report",
    }
    reference_manifest = json.loads(
        reference.artifacts["reference-library"].path.read_text(encoding="utf-8")
    )
    checkpoint_manifest = json.loads(
        implementation.artifacts["checkpoint"].path.read_text(encoding="utf-8")
    )
    assert reference_manifest["root"] == "reference.ndm"
    assert checkpoint_manifest["root"] == "routed.ndm"
    assert reference_manifest["members"][0]["path"] == "library.ndm"
    assert checkpoint_manifest["members"][0]["path"] == "top.ndm"
    assert implementation.facts == {"passed": True}
    for record in result.run_root.rglob("*.json"):
        assert str(tmp_path) not in record.read_text(encoding="utf-8")


def test_synopsys_fc_adapter_records_owner_runner_failure(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root, fail_pnr=True)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))

    result = engine.run(
        engine.plan(spec, "implementation", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="b" * 32,
    )

    assert result.status == "failed"
    assert result.nodes["reference-library"].status == "accepted"
    implementation = result.nodes["implementation"]
    assert implementation.execution_status == "failed"
    assert implementation.result_status == "failed"
    assert implementation.artifacts == {}
    action_result = json.loads(
        (
            result.run_root
            / "nodes"
            / "implementation"
            / "action_result.json"
        ).read_text(encoding="utf-8")
    )
    assert action_result["execution"]["exit_code"] == 7
    assert str(tmp_path) not in json.dumps(action_result)


def test_synopsys_fc_adapter_rejects_stale_reference_before_downstream_use(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    environment, _collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))
    plan = engine.plan(spec, "implementation", profile)
    first = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="d" * 32,
    )
    reference_artifact = first.nodes["reference-library"].artifacts[
        "reference-library"
    ]
    reference_manifest = json.loads(
        reference_artifact.path.read_text(encoding="utf-8")
    )
    reference_member = (
        reference_artifact.path.parent
        / reference_manifest["root"]
        / reference_manifest["members"][0]["path"]
    )
    reference_member.write_text("stale reference library\n", encoding="utf-8")
    first.nodes["implementation"].artifacts["routed-netlist"].path.write_text(
        "stale routed netlist\n",
        encoding="utf-8",
    )

    resumed = engine.run(
        plan,
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="d" * 32,
        resume=True,
    )

    assert resumed.nodes["reference-library"].reused is True
    implementation = resumed.nodes["implementation"]
    assert implementation.status == "failed"
    assert implementation.execution_status == "failed"
    assert "missing or stale" in (implementation.reason or "")


def test_synopsys_fc_preflight_rejects_stale_recipe_and_platform(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    environment, collateral = _environment(tmp_path)
    engine = FlowEngine(_registry(owner_root))
    plan = engine.plan(spec, "implementation", profile)

    (owner_root / "place-route.tcl").write_text(
        "# stale implementation fixture\n",
        encoding="utf-8",
    )
    source_preflight = engine.preflight(plan, environment)
    assert source_preflight.status == "blocked"
    assert any(
        check.requirement_kind == "source-revision" and check.status == "stale"
        for check in source_preflight.checks
    )
    with pytest.raises(FlowExecutionError, match="preflight is blocked"):
        engine.run(
            plan,
            artifact_root=tmp_path / "source-artifacts",
            environment=environment,
            run_id="c" * 32,
        )
    assert not (tmp_path / "source-artifacts").exists()

    clean_plan = engine.plan(spec, "implementation", profile)
    collateral["tluplus"].write_text("stale TLU+ fixture\n", encoding="utf-8")
    platform_preflight = engine.preflight(clean_plan, environment)
    assert platform_preflight.status == "blocked"
    assert any(
        check.requirement == "physical-technology" and check.status == "stale"
        for check in platform_preflight.checks
    )
