from __future__ import annotations

import json
from pathlib import Path

import pytest

from sigilicon.cli.agentic_execute import main as execute_cli_main
from sigilicon.cli.agentic_read import main as read_cli_main
from sigilicon.domain.agentic_execution import (
    AgenticExecutionCapability,
    AgenticExecutionGrant,
)
from sigilicon.domain.circuit_design import (
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    DESIGN_EVIDENCE_KIND,
    EvidenceLevel,
    EvidenceRole,
)
from sigilicon.flow import ActionContract, ArtifactPort, FlowRegistry
from sigilicon.workflows import agentic_read as agentic_read_module
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.design_campaign import (
    DesignArtifactBinding,
    DesignCampaignAttemptSpec,
    DesignCampaignBudget,
    DesignCampaignScope,
    DesignCampaignSpec,
    DesignStage,
    DesignStageBinding,
)

from test_agentic_read_interface import write_read_only_flow_project
from test_design_campaign import ACTION, POLICY, AttemptAdapter


def _write_campaign_project(root: Path) -> None:
    write_read_only_flow_project(root)
    flow_root = root / "ip/example/configs/flows"
    (flow_root / "pipeline.toml").write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "attempt"
action = "design.attempt"

[[targets]]
name = "all"
goals = ["attempt"]
''',
        encoding="utf-8",
    )
    (flow_root / "profiles/offline.toml").write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "offline"

[actions."design.attempt"]
adapter = "typed-attempt"
''',
        encoding="utf-8",
    )


def _registry(original, owner_root: Path | None) -> FlowRegistry:
    registry = original(owner_root)
    registry.register_action(
        ActionContract(
            ACTION,
            outputs=(
                ArtifactPort("candidate", DESIGN_CANDIDATE_KIND),
                ArtifactPort("topology", CIRCUIT_TOPOLOGY_KIND),
                ArtifactPort("l0-evidence", DESIGN_EVIDENCE_KIND),
            ),
            adapters=("typed-attempt",),
        )
    )
    from sigilicon.domain.circuit_design import EvidenceConclusion

    registry.register_adapter(
        "typed-attempt",
        AttemptAdapter(EvidenceConclusion.SATISFIED),
    )
    return registry


def _campaign() -> DesignCampaignSpec:
    return DesignCampaignSpec(
        "example",
        "pilot-campaign",
        (
            DesignCampaignAttemptSpec(
                "baseline",
                "pipeline",
                "all",
                "offline",
                DesignArtifactBinding("candidate", "attempt", "candidate"),
                (
                    DesignArtifactBinding("topology", "attempt", "topology"),
                    DesignArtifactBinding("l0", "attempt", "l0-evidence"),
                ),
                (DesignStageBinding(DesignStage.L0, "l0"),),
                None,
            ),
        ),
        DesignCampaignBudget(1, 1, 1),
        DesignCampaignScope(
            (DesignStage.L0,),
            EvidenceRole.DIAGNOSTIC,
            EvidenceLevel.L0,
            ("functional",),
            POLICY,
        ),
    )


def _grant(campaign_identity: str) -> AgenticExecutionGrant:
    return AgenticExecutionGrant(
        principal="test-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plan_sha256=(campaign_identity,),
        approval="campaign-test-approval",
        expires_at="2099-01-01T00:00:00+00:00",
    )


def test_campaign_python_cli_share_plan_execution_and_immutable_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_campaign_project(tmp_path)
    original = agentic_read_module.builtin_workflow_registry
    monkeypatch.setattr(
        agentic_read_module,
        "builtin_workflow_registry",
        lambda owner_root: _registry(original, owner_root),
    )
    source = _campaign()
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(source.canonical_json(), encoding="utf-8")
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]

    assert read_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "campaign-plan",
            "--campaign",
            str(campaign_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == planned

    grant = _grant(campaign_identity)
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(grant.canonical_json(), encoding="utf-8")
    execution = AgenticExecutionInterface(read, grant=grant)
    result = execution.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    )

    assert result["operation"] == "campaign.run"
    assert result["conclusion"] == "passed"
    assert result["data"]["result"]["termination"] == "passed"
    assert result["data"]["result"]["final_decision"]["role"] == "diagnostic"
    assert str(tmp_path) not in json.dumps(result)
    assert execution.run_campaign(
        campaign_json=source.canonical_json(),
        campaign_identity=campaign_identity,
    ) == result

    assert execute_cli_main(
        [
            "--project-root",
            str(tmp_path),
            "--grant",
            str(grant_path),
            "campaign-run",
            campaign_identity,
            "--campaign",
            str(campaign_path),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == result
    audits = tuple((tmp_path / "artifacts").rglob("audit.json"))
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["campaign_identity"] == campaign_identity
    assert audit["approval"] == "campaign-test-approval"
    assert audit["terminal_status"] == "passed"


def test_campaign_run_requires_exact_identity_and_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    original = agentic_read_module.builtin_workflow_registry
    monkeypatch.setattr(
        agentic_read_module,
        "builtin_workflow_registry",
        lambda owner_root: _registry(original, owner_root),
    )
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    campaign_identity = read.plan_campaign(
        campaign_json=source.canonical_json()
    )["data"]["campaign_identity"]
    execution = AgenticExecutionInterface(read, grant=_grant("f" * 64))

    with pytest.raises(ValueError, match="approved"):
        execution.run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity=campaign_identity,
        )
    with pytest.raises(ValueError, match="identity drift"):
        AgenticExecutionInterface(read, grant=_grant("f" * 64)).run_campaign(
            campaign_json=source.canonical_json(),
            campaign_identity="f" * 64,
        )
    assert not (tmp_path / "artifacts").exists()
