from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

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
    PolicyCheck,
    PolicySpec,
    ResolvedCapability,
    ResolvedPlatformAsset,
    ResolvedPlatformAssetMember,
    SourceAssetsAdapter,
    SynopsysHSpiceAdapter,
    register_standard_asic_actions,
)


def _write_owner(owner_root: Path) -> None:
    (owner_root / "electrical").mkdir(parents=True)
    (owner_root / "decks").mkdir()
    (owner_root / "electrical/core.sp").write_text(
        ".subckt core in out\n.ends core\n",
        encoding="utf-8",
    )
    (owner_root / "decks/polarity.sp").write_text(
        "fixture polarity deck\n",
        encoding="utf-8",
    )
    runner = owner_root / "run-hspice-fixture.py"
    runner.write_text(
        '''#!/usr/bin/env python3
import os
from pathlib import Path
import sys

assert sys.argv[1] == "calibrated-polarity"
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "paper_0p8v"
assert os.environ["SIGILICON_DESIGN_CORNER"] == "tt0p8v25c"
for name in (
    "SIGILICON_SYNOPSYS_HSPICE",
    "SIGILICON_HSPICE_NOMINAL_MODEL",
    "SIGILICON_STDCELL_RVT_SPICE",
    "SIGILICON_STDCELL_HVT_SPICE",
    "SIGILICON_STDCELL_LVT_SPICE",
):
    assert Path(os.environ[name]).is_file()
for name in ("SIGILICON_HSPICE_SOURCE_ROOT", "SIGILICON_HSPICE_DECK_ROOT"):
    root = Path(os.environ[name])
    assert root.is_dir() and list(root.rglob("*"))
output = Path(os.environ["SIGILICON_HSPICE_OUTPUT_ROOT"])
output.mkdir(parents=True, exist_ok=True)
mode = os.environ.get("SIGILICON_HSPICE_FIXTURE_MODE", "valid")
if mode == "tool-failed":
    raise SystemExit(7)
if mode == "missing":
    raise SystemExit(0)
header = "$OPTION MEASFORM=3\\n.TITLE 'fixture'\\n"
if mode == "malformed":
    (output / "calibrated-polarity.mt0.csv").write_text(
        header + "vcm,pos_margin\\n0.350,not-a-number\\n",
        encoding="utf-8",
    )
    raise SystemExit(0)
margin = "-0.1" if mode == "negative-margin" else "0.7998"
if mode == "failed-measurement":
    margin = "failed"
(output / "calibrated-polarity.mt0.csv").write_text(
    header
    + "index,vcm,pos_margin,neg_margin,temper,alter#\\n"
    + f"1,0.350,{margin},0.7998,25.0,1\\n"
    + "2,0.355,0.7998,0.7998,25.0,1\\n",
    encoding="utf-8",
)
''',
        encoding="utf-8",
    )
    runner.chmod(0o755)
    (owner_root / "source-assets.toml").write_text(
        '''schema = 1
contract_kind = "source-assets"
path_scope = "owner"
owner = "fixture"
name = "hspice-fixture-source"

[qualifiers]
variant = "paper_0p8v"
corner = "tt0p8v25c"

[[artifacts]]
role = "electrical-sources"
kind = "source-set.spice"
materialization = "manifest"
members = ["electrical/core.sp"]

[[artifacts]]
role = "decks"
kind = "source-set.spice-deck"
materialization = "manifest"
members = ["decks/polarity.sp"]

[[artifacts]]
role = "electrical-recipe"
kind = "recipe.electrical-simulation"
materialization = "manifest"
members = ["run-hspice-fixture.py"]
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
            kind="design.hspice-fixture",
            outputs=(
                ArtifactPort("electrical-sources", "source-set.spice"),
                ArtifactPort("decks", "source-set.spice-deck"),
                ArtifactPort(
                    "electrical-recipe",
                    "recipe.electrical-simulation",
                ),
            ),
            adapters=("source-assets",),
            resolves_source_assets=True,
        )
    )
    register_standard_asic_actions(registry)
    registry.register_adapter("source-assets", SourceAssetsAdapter())
    registry.register_adapter("synopsys-hspice", SynopsysHSpiceAdapter(owner_root))
    return registry


def _flow(owner_root: Path) -> tuple[FlowSpec, ExecutionProfile]:
    spec = FlowSpec(
        owner="fixture",
        flow_id="hspice-managed",
        nodes=(
            FlowNode(
                "assets",
                "design.hspice-fixture",
                config={"source": "source-assets.toml"},
            ),
            FlowNode(
                "electrical-functional",
                "asic.electrical-functional",
                config={
                    "runner": "run-hspice-fixture.py",
                    "target": "calibrated-polarity",
                    "model_section": "TOP_TT",
                    "measurement_file": "calibrated-polarity.mt0.csv",
                    "required_measurements": (
                        "vcm",
                        "pos_margin",
                        "neg_margin",
                    ),
                    "positive_measurements": ("pos_margin", "neg_margin"),
                },
                bindings=(
                    ArtifactBinding(
                        "electrical-sources",
                        "assets",
                        "electrical-sources",
                    ),
                    ArtifactBinding("decks", "assets", "decks"),
                    ArtifactBinding(
                        "electrical-recipe",
                        "assets",
                        "electrical-recipe",
                    ),
                ),
                policy="electrical-functional-regression",
            ),
        ),
        targets=(FlowTarget("electrical-regression", ("electrical-functional",)),),
        policies=(
            PolicySpec(
                "electrical-functional-regression",
                (
                    PolicyCheck(
                        "tool-completed",
                        "tool-execution-completed",
                        "equals",
                        True,
                    ),
                    PolicyCheck("one-file", "measurement-file-count", "equals", 1),
                    PolicyCheck("two-rows", "measurement-row-count", "equals", 2),
                    PolicyCheck(
                        "no-failed-values",
                        "measurement-failure-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "polarity-holds",
                        "measurement-check-failure-count",
                        "at_most",
                        0,
                    ),
                ),
            ),
        ),
        owner_root=owner_root,
    )
    profile = ExecutionProfile(
        owner="fixture",
        profile_id="hspice-fixture",
        selections=(
            AdapterSelection("design.hspice-fixture", "source-assets"),
            AdapterSelection(
                "asic.electrical-functional",
                "synopsys-hspice",
                config={"timeout_seconds": 30},
                platform_asset_identities={
                    "hspice-models": "fixture:hspice@tt0p8v25c",
                },
            ),
        ),
    )
    return spec, profile


def _environment(tmp_path: Path, executable: Path) -> ExecutionEnvironment:
    members: list[ResolvedPlatformAssetMember] = []
    for role in ("nominal-model", "rvt", "hvt", "lvt"):
        model = tmp_path / f"models/{role}.sp"
        model.parent.mkdir(exist_ok=True)
        model.write_text(f"* {role} fixture\n", encoding="utf-8")
        members.append(
            ResolvedPlatformAssetMember(
                role=role,
                digest=sha256(model.read_bytes()).hexdigest(),
                location=model,
            )
        )
    return ExecutionEnvironment(
        capabilities={
            "tool.synopsys-hspice": ResolvedCapability(
                "hspice-fixture",
                executable=executable,
            )
        },
        platform_assets=(
            ResolvedPlatformAsset(
                role="hspice-models",
                kind="model.hspice-set",
                identity="fixture:hspice@tt0p8v25c",
                members=tuple(members),
            ),
        ),
    )


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    engine = FlowEngine(_registry(owner_root))
    environment = _environment(tmp_path, owner_root / "run-hspice-fixture.py")
    monkeypatch.setenv("SIGILICON_HSPICE_FIXTURE_MODE", mode)
    return engine.run(
        engine.plan(spec, "electrical-regression", profile),
        artifact_root=tmp_path / "artifacts",
        environment=environment,
        run_id="1" * 32,
    )


def test_synopsys_hspice_adapter_collects_structured_measurements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(tmp_path, monkeypatch, "valid")
    outcome = result.nodes["electrical-functional"]
    measurement = json.loads(outcome.artifacts["measurements"].path.read_text())

    assert result.status == "accepted"
    assert outcome.facts == {
        "measurement-check-failure-count": 0,
        "measurement-failure-count": 0,
        "measurement-file-count": 1,
        "measurement-row-count": 2,
        "tool-execution-completed": True,
    }
    assert measurement["kind"] == "measurement.collection"
    assert measurement["measurement_file"] == "calibrated-polarity.mt0.csv"
    assert measurement["rows"][0]["vcm"] == 0.35
    assert str(tmp_path) not in json.dumps(measurement)


@pytest.mark.parametrize("mode", ["tool-failed", "missing", "malformed"])
def test_synopsys_hspice_adapter_fails_closed_on_invalid_execution_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    result = _run(tmp_path, monkeypatch, mode)

    assert result.status == "failed"
    assert result.nodes["electrical-functional"].status == "failed"


def test_synopsys_hspice_adapter_leaves_owner_measurement_checks_to_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(tmp_path, monkeypatch, "negative-margin")
    outcome = result.nodes["electrical-functional"]

    assert result.status == "failed"
    assert outcome.status == "rejected"
    assert outcome.result_status == "valid"
    assert outcome.facts["measurement-check-failure-count"] == 1


def test_synopsys_hspice_adapter_reports_hspice_failed_measurements_to_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(tmp_path, monkeypatch, "failed-measurement")
    outcome = result.nodes["electrical-functional"]

    assert result.status == "failed"
    assert outcome.status == "rejected"
    assert outcome.result_status == "valid"
    assert outcome.facts["measurement-failure-count"] == 1
    assert outcome.facts["measurement-check-failure-count"] == 1


def test_hspice_preflight_rejects_wrong_corner_model_identity(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    _write_owner(owner_root)
    spec, profile = _flow(owner_root)
    engine = FlowEngine(_registry(owner_root))
    environment = _environment(tmp_path, owner_root / "run-hspice-fixture.py")
    wrong_asset = ResolvedPlatformAsset(
        role="hspice-models",
        kind="model.hspice-set",
        identity="fixture:hspice@tt0p9v25c",
        members=environment.platform_assets[0].members,
    )
    wrong_environment = ExecutionEnvironment(
        capabilities=environment.capabilities,
        platform_assets=(wrong_asset,),
    )

    preflight = engine.preflight(
        engine.plan(spec, "electrical-regression", profile),
        wrong_environment,
    )

    assert preflight.status == "blocked"
    model_check = next(
        check for check in preflight.checks if check.requirement == "hspice-models"
    )
    assert model_check.expected == {
        "kind": "model.hspice-set",
        "identity": "fixture:hspice@tt0p8v25c",
    }
    assert model_check.identity == "fixture:hspice@tt0p9v25c"
