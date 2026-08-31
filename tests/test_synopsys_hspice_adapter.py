from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sigilicon.flow import (
    ActionBinding,
    ActionContract,
    ArtifactBinding,
    ArtifactPort,
    ExecutionEnvironment,
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
)
from sigilicon.flow.standard_asic import register_standard_asic_actions
from sigilicon.flow.source_assets import SourceAssetsAdapter
from sigilicon.workflows.synopsys.hspice import SynopsysHSpiceAdapter


def _write_owner(owner_root: Path) -> None:
    (owner_root / "electrical").mkdir(parents=True)
    (owner_root / "decks").mkdir()
    (owner_root / "electrical/core.sp").write_text(
        ".subckt core in out\n.ends core\n",
        encoding="utf-8",
    )
    (owner_root / "decks/smoke.sp").write_text(
        "fixture smoke deck\n",
        encoding="utf-8",
    )
    runner = owner_root / "run-hspice-fixture.py"
    runner.write_text(
        '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

target = sys.argv[1]
assert target in {"smoke", "campaign", "diagnostic"}
assert os.environ["SIGILICON_DESIGN_VARIANT"] == "fixture_variant"
assert os.environ["SIGILICON_DESIGN_CORNER"] == "nominal"
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
if target == "diagnostic":
    assert os.environ["FIXTURE_RUN_MODE"] == "managed"
    assert Path(os.environ["SIGILICON_HSPICE_MISMATCH_MODEL"]).is_file()
    raise SystemExit(0)
if target == "campaign":
    assert os.environ["FIXTURE_BATCH_SIZE"] == "2"
    assert os.environ["FIXTURE_RUN_MODE"] == "managed"
    assert Path(os.environ["SIGILICON_HSPICE_MISMATCH_MODEL"]).is_file()
    summary = {
        "schema": 1,
        "contract_kind": "fixture-campaign-summary",
        "status": "complete",
        "observations": [{"index": 1}, {"index": 2}],
    }
    campaign = output / "campaign"
    campaign.mkdir()
    (campaign / "summary.json").write_text(
        json.dumps(summary) + "\\n", encoding="utf-8"
    )
    raise SystemExit(0)
header = "$OPTION MEASFORM=3\\n.TITLE 'fixture'\\n"
if mode == "malformed":
    (output / "smoke.mt0.csv").write_text(
        header + "stimulus,positive_margin\\n0.25,not-a-number\\n",
        encoding="utf-8",
    )
    raise SystemExit(0)
margin = "-0.1" if mode == "negative-margin" else "0.75"
if mode == "failed-measurement":
    margin = "failed"
(output / "smoke.mt0.csv").write_text(
    header
    + "index,stimulus,positive_margin,negative_margin,temper,alter#\\n"
    + f"1,0.25,{margin},0.75,25.0,1\\n"
    + "2,0.50,0.75,0.75,25.0,1\\n",
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
owner = "fixture-owner"
name = "hspice-fixture-source"

[qualifiers]
variant = "fixture_variant"
corner = "nominal"

[[artifacts]]
role = "electrical-sources"
kind = "source-set.spice"
materialization = "manifest"
members = ["electrical/core.sp"]

[[artifacts]]
role = "decks"
kind = "source-set.spice-deck"
materialization = "manifest"
members = ["decks/smoke.sp"]

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
    registry.register_adapter("synopsys-hspice", SynopsysHSpiceAdapter())
    return registry


def _flow(
    owner_root: Path,
    *,
    runner_environment: object | None = None,
) -> FlowSpec:
    campaign_environment = (
        {"FIXTURE_BATCH_SIZE": 2, "FIXTURE_RUN_MODE": "managed"}
        if runner_environment is None
        else runner_environment
    )
    spec = FlowSpec(
        owner="fixture-owner",
        flow_id="hspice-managed",
        recipe_id="hspice-managed-recipe",
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
                    "target": "smoke",
                    "model_section": "NOMINAL",
                    "measurement_file": "smoke.mt0.csv",
                    "required_measurements": (
                        "stimulus",
                        "positive_margin",
                        "negative_margin",
                    ),
                    "positive_measurements": (
                        "positive_margin",
                        "negative_margin",
                    ),
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
            FlowNode(
                "electrical-campaign",
                "asic.electrical-campaign",
                config={
                    "runner": "run-hspice-fixture.py",
                    "target": "campaign",
                    "model_section": "MISMATCH",
                    "summary_file": "campaign/summary.json",
                    "summary_contract_kind": "fixture-campaign-summary",
                    "summary_records_field": "observations",
                    "runner_environment_prefix": "FIXTURE_",
                    "runner_environment": campaign_environment,
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
                policy="electrical-campaign-complete",
            ),
            FlowNode(
                "electrical-diagnostic",
                "asic.electrical-diagnostic",
                config={
                    "runner": "run-hspice-fixture.py",
                    "target": "diagnostic",
                    "model_section": "MISMATCH",
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
                policy="electrical-diagnostic-complete",
            ),
        ),
        targets=(
            FlowTarget("electrical-regression", ("electrical-functional",)),
            FlowTarget("campaign", ("electrical-campaign",)),
            FlowTarget("diagnostic", ("electrical-diagnostic",)),
        ),
        policies=(
            PolicySpec(
                "electrical-functional-regression",
                (
                    PolicyCheck("two-rows", "measurement-row-count", "equals", 2),
                    PolicyCheck(
                        "no-failed-values",
                        "measurement-failure-count",
                        "at_most",
                        0,
                    ),
                    PolicyCheck(
                        "positive-margins",
                        "measurement-check-failure-count",
                        "at_most",
                        0,
                    ),
                ),
            ),
            PolicySpec(
                "electrical-campaign-complete",
                (
                    PolicyCheck(
                        "two-records",
                        "campaign-record-count",
                        "equals",
                        2,
                    ),
                ),
            ),
            PolicySpec(
                "electrical-diagnostic-complete",
                (
                    PolicyCheck(
                        "diagnostic-role",
                        "evidence-role",
                        "equals",
                        "diagnostic",
                    ),
                    PolicyCheck(
                        "not-qualification",
                        "product-qualification-conclusion",
                        "equals",
                        False,
                    ),
                ),
            ),
        ),
        owner_root=owner_root,
        action_bindings=(
            ActionBinding("design.hspice-fixture", "source-assets"),
            ActionBinding(
                "asic.electrical-functional",
                "synopsys-hspice",
                config={"timeout_seconds": 30},
                platform_assets={
                    "hspice-models": "fixture:hspice@nominal",
                },
            ),
            ActionBinding(
                "asic.electrical-campaign",
                "synopsys-hspice",
                config={"timeout_seconds": 30},
                platform_assets={
                    "hspice-models": "fixture:hspice@nominal",
                },
            ),
            ActionBinding(
                "asic.electrical-diagnostic",
                "synopsys-hspice",
                config={
                    "timeout_seconds": 30,
                    "runner_environment_prefix": "FIXTURE_",
                    "runner_environment": {"FIXTURE_RUN_MODE": "managed"},
                },
                platform_assets={
                    "hspice-models": "fixture:hspice@nominal",
                },
            ),
        ),
    )
    return spec


def _environment(tmp_path: Path, executable: Path) -> ExecutionEnvironment:
    members: list[ResolvedPlatformAssetMember] = []
    for role in (
        "nominal-model",
        "mismatch-model",
        "rvt",
        "hvt",
        "lvt",
        "stdcell-12t-rvt",
    ):
        model = tmp_path / f"models/{role}.sp"
        model.parent.mkdir(exist_ok=True)
        model.write_text(f"* {role} fixture\n", encoding="utf-8")
        members.append(
            ResolvedPlatformAssetMember(
                role=role,
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
                identity="fixture:hspice@nominal",
                members=tuple(members),
            ),
        ),
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    target: str = "electrical-regression",
    runner_environment: object | None = None,
):
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    _write_owner(owner_root)
    spec = _flow(
        owner_root,
        runner_environment=runner_environment,
    )
    engine = FlowEngine(_registry(owner_root))
    environment = _environment(tmp_path, owner_root / "run-hspice-fixture.py")
    monkeypatch.setenv("SIGILICON_HSPICE_FIXTURE_MODE", mode)
    return engine.run(
        engine.plan(spec, target),
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
    assert outcome.facts.as_mapping() == {
        "measurement-check-failure-count": 0,
        "measurement-failure-count": 0,
        "measurement-row-count": 2,
    }
    assert measurement["kind"] == "measurement.collection"
    assert measurement["measurement_file"] == "smoke.mt0.csv"
    assert measurement["rows"][0]["stimulus"] == 0.25
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


def test_synopsys_hspice_adapter_reports_failed_measurements_to_policy(
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


def test_hspice_preflight_rejects_wrong_model_identity(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    _write_owner(owner_root)
    spec = _flow(owner_root)
    engine = FlowEngine(_registry(owner_root))
    environment = _environment(tmp_path, owner_root / "run-hspice-fixture.py")
    wrong_asset = ResolvedPlatformAsset(
        role="hspice-models",
        kind="model.hspice-set",
        identity="fixture:hspice@alternate",
        members=environment.platform_assets[0].members,
    )
    wrong_environment = ExecutionEnvironment(
        capabilities=environment.capabilities,
        platform_assets=(wrong_asset,),
    )

    preflight = engine.preflight(
        engine.plan(spec, "electrical-regression"),
        wrong_environment,
    )

    assert preflight.status == "blocked"
    model_check = next(
        check for check in preflight.checks if check.requirement == "hspice-models"
    )
    assert model_check.expected == {
        "kind": "model.hspice-set",
        "identity": "fixture:hspice@nominal",
    }
    assert model_check.identity == "fixture:hspice@alternate"


def test_managed_campaign_uses_owner_declared_summary_interface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(tmp_path, monkeypatch, "valid", target="campaign")
    campaign = result.nodes["electrical-campaign"]
    summary = json.loads(campaign.artifacts["campaign-summary"].path.read_text())

    assert result.status == "accepted"
    assert campaign.facts.as_mapping() == {
        "campaign-record-count": 2,
    }
    assert summary["contract_kind"] == "fixture-campaign-summary"
    assert summary["kind"] == "report.electrical-campaign"
    assert len(summary["observations"]) == 2
    assert str(tmp_path) not in json.dumps(summary)


def test_managed_diagnostic_is_typed_nonqualification_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(tmp_path, monkeypatch, "valid", target="diagnostic")
    diagnostic = result.nodes["electrical-diagnostic"]
    evidence = json.loads(diagnostic.artifacts["evidence"].path.read_text())

    assert result.status == "accepted"
    assert diagnostic.facts.as_mapping() == {
        "evidence-role": "diagnostic",
        "product-qualification-conclusion": False,
    }
    assert evidence["target"] == "diagnostic"
    assert evidence["tool_execution_completed"] is True
    assert evidence["product_qualification_conclusion"] is False


def test_campaign_rejects_framework_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        tmp_path,
        monkeypatch,
        "valid",
        target="campaign",
        runner_environment={"SIGILICON_DESIGN_VARIANT": "forged"},
    )

    assert result.status == "failed"
    outcome = result.nodes["electrical-campaign"]
    assert outcome.status == "failed"
    assert "cannot override SIGILICON_*" in outcome.reason


def test_campaign_rejects_reserved_process_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        tmp_path,
        monkeypatch,
        "valid",
        target="campaign",
        runner_environment={"PATH": "/forged"},
    )

    assert result.status == "failed"
    outcome = result.nodes["electrical-campaign"]
    assert outcome.status == "failed"
    assert "cannot override PATH" in outcome.reason
