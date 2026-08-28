from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

mcp = pytest.importorskip("mcp")

from conftest import write_component_owner, write_project_context
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.exceptions import MCPError
from sigilicon.integrations.mcp.server import create_server
from sigilicon.canonical import canonical_json
from sigilicon.domain.circuit_design import (
    ARTIFACT_SCHEMA,
    CIRCUIT_TOPOLOGY_KIND,
    DESIGN_CANDIDATE_KIND,
    SOURCE_NETLIST_KIND,
    ArtifactMetadata,
    ArtifactReference,
    CircuitPort,
    CircuitTopologyProposal,
    DesignCandidate,
    PortDirection,
    ProposalProvenance,
    TopologyOrigin,
)
from sigilicon.domain.agentic_execution import (
    AgenticExecutionBudget,
    AgenticExecutionCapability,
    AgenticExecutionGrant,
    AgenticPlanApproval,
)
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows import agentic_read as agentic_read_module
from sigilicon.workflows.design_campaign import design_campaign_state_from_json
from sigilicon.workflows.design_repair import DesignRepairProposal

from test_agentic_campaign_interface import (
    _campaign,
    _feedback_campaign,
    _feedback_registry,
    _grant as campaign_grant,
    _registry as campaign_registry,
    _write_campaign_project,
)
from test_design_promotion import _inputs as promotion_inputs
from test_design_campaign import _topology


def write_mcp_project(root: Path) -> None:
    owner_root = root / "ip/example"
    flow_root = owner_root / "configs/flows"
    profile_root = flow_root / "profiles"
    profile_root.mkdir(parents=True)
    (flow_root / "pipeline.toml").write_text(
        '''schema = 1
contract_kind = "flow"
path_scope = "owner"
owner = "example"
name = "pipeline"

[[nodes]]
id = "source"
action = "fake.source"
config = { text = "hello" }

[[targets]]
name = "all"
goals = ["source"]
''',
        encoding="utf-8",
    )
    (profile_root / "offline.toml").write_text(
        '''schema = 1
contract_kind = "execution-profile"
path_scope = "owner"
owner = "example"
name = "offline"

[actions."fake.source"]
adapter = "fake-source"
''',
        encoding="utf-8",
    )
    catalog = flow_root / "catalog.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "flow-catalog"
path_scope = "owner"
owner = "example"

[flows.pipeline]
contract = "configs/flows/pipeline.toml"
default_profile = "offline"

[flows.pipeline.profiles]
offline = "configs/flows/profiles/offline.toml"
''',
        encoding="utf-8",
    )
    write_component_owner(
        root,
        "example",
        filesets={
            "flow": tuple(
                path.relative_to(root).as_posix()
                for path in (
                    catalog,
                    flow_root / "pipeline.toml",
                    profile_root / "offline.toml",
                )
            )
        },
    )


def candidate_chain() -> tuple[str, list[str]]:
    topology = CircuitTopologyProposal(
        ArtifactMetadata(ARTIFACT_SCHEMA, CIRCUIT_TOPOLOGY_KIND, "example", "example:topology:native-mcp"),
        "inv",
        "source-fixture",
        TopologyOrigin.PROPOSED,
        (CircuitPort("IN", PortDirection.INPUT, "signal"),),
        (),
        (),
        (),
        ProposalProvenance("test-proposal", "1"),
    )
    candidate = DesignCandidate(
        ArtifactMetadata(ARTIFACT_SCHEMA, DESIGN_CANDIDATE_KIND, "example", "example:candidate:native-mcp"),
        "inv",
        ArtifactReference("example", SOURCE_NETLIST_KIND, "source-fixture", None),
        None,
        (),
        topology.reference(),
        None,
        None,
        (),
        None,
        topology.provenance,
    )
    return candidate.canonical_json(), [topology.canonical_json()]


def test_native_mcp_is_strict_read_only_and_matches_python(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    interface = AgenticReadInterface.from_project_root(tmp_path)
    server = create_server(interface)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "project.inspect",
                "flow.plan",
                "run.inspect",
                "candidate.validate",
                "campaign.plan",
                "candidate.promotion_plan",
            ]
            assert all(
                tool.annotations is not None
                and tool.annotations.read_only_hint is True
                and tool.annotations.open_world_hint is False
                and tool.input_schema.get("additionalProperties") is False
                for tool in listed.tools
            )

            expected = interface.plan_flow(
                owner="example",
                flow="pipeline",
                target="all",
                profile=None,
            )
            result = await client.call_tool(
                "flow.plan",
                {"owner": "example", "flow": "pipeline", "target": "all"},
            )
            assert result.is_error is False
            assert result.structured_content == expected

            unknown = await client.call_tool(
                "project.inspect",
                {"owner": "example", "unexpected": "ignored?"},
            )
            assert unknown.is_error is True
            assert unknown.structured_content["conclusion"] == "non-conclusion"

            injected = await client.call_tool(
                "flow.plan",
                {
                    "owner": "example",
                    "flow": "pipeline; touch owned",
                    "target": "all",
                },
            )
            assert injected.is_error is True
            assert not (tmp_path / "owned").exists()

            candidate_json, artifacts = candidate_chain()
            candidate = await client.call_tool(
                "candidate.validate",
                {
                    "owner": "example",
                    "candidate": candidate_json,
                    "artifacts": artifacts,
                },
            )
            assert candidate.is_error is False
            assert candidate.structured_content == interface.validate_candidate(
                owner="example",
                candidate_json=candidate_json,
                artifact_json=tuple(artifacts),
            )

            promoted_candidate, topology, evidence, decision, request = promotion_inputs()
            promotion_arguments = {
                "owner": "example",
                "candidate": promoted_candidate.canonical_json(),
                "artifacts": [topology.canonical_json(), evidence.canonical_json()],
                "decision": decision.canonical_json(),
                "request": request.canonical_json(),
            }
            promotion = await client.call_tool(
                "candidate.promotion_plan",
                promotion_arguments,
            )
            assert promotion.is_error is False
            assert promotion.structured_content == interface.plan_candidate_promotion(
                owner="example",
                candidate_json=promotion_arguments["candidate"],
                artifact_json=tuple(promotion_arguments["artifacts"]),
                decision_json=promotion_arguments["decision"],
                request_json=promotion_arguments["request"],
            )
            assert promotion.structured_content["data"]["writes_canonical_source"] is False

            resources = await client.list_resources()
            assert [str(item.uri) for item in resources.resources] == [
                interface.project_resource_uri
            ]
            resource = await client.read_resource(interface.project_resource_uri)
            payload = json.loads(resource.contents[0].text)
            assert payload == interface.inspect_project(owner=None)

            templates = await client.list_resource_templates()
            assert [item.name for item in templates.resource_templates] == [
                "owner-catalog",
                "flow-run-result",
            ]
            with pytest.raises(MCPError):
                await client.read_resource("sigilicon://owners/../catalog")

    asyncio.run(scenario())


def test_native_mcp_stdio_entrypoint_round_trips(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    expected = AgenticReadInterface.from_project_root(tmp_path).inspect_project(
        owner="example"
    )
    python_path = os.environ.get("PYTHONPATH", "")

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "sigilicon.integrations.mcp.stdio",
                "--project-root",
                str(tmp_path),
            ],
            env={"PYTHONPATH": python_path},
        )
        async with Client(parameters) as client:
            result = await client.call_tool(
                "project.inspect",
                {"owner": "example"},
            )
            assert result.is_error is False
            assert result.structured_content == expected

    asyncio.run(scenario())


def test_native_mcp_execution_is_grant_filtered_and_matches_python(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    read = AgenticReadInterface.from_project_root(tmp_path)
    plan = read.plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile="offline",
    )
    plan_identity = plan["data"]["plan_identity"]
    grant = AgenticExecutionGrant(
        principal="test-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plans=(
            AgenticPlanApproval(plan_identity, canonical_json(plan["data"]["plan"])),
        ),
        approval="mcp-execution-test",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    execution = AgenticExecutionInterface(read, grant=grant)
    server = create_server(execution)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == {
                "project.inspect",
                "flow.plan",
                "flow.run",
                "run.inspect",
                "run.cancel",
                "candidate.validate",
                "campaign.plan",
                "candidate.promotion_plan",
                "campaign.run",
            }
            assert tools["flow.run"].annotations.read_only_hint is False
            assert tools["flow.run"].annotations.destructive_hint is True
            assert tools["run.cancel"].annotations.read_only_hint is False
            assert tools["run.cancel"].annotations.destructive_hint is True
            assert tools["campaign.plan"].annotations.read_only_hint is True
            assert tools["campaign.run"].annotations.destructive_hint is True
            run_id_patterns = {
                tools["run.inspect"].input_schema["properties"]["run_id"]["pattern"],
                tools["run.cancel"].input_schema["properties"]["run_id"]["pattern"],
                tools["campaign.run"].input_schema["properties"]["run_id"]["pattern"],
            }
            assert len(run_id_patterns) == 1
            assert set(tools["flow.run"].input_schema["properties"]) == {
                "plan_identity",
                "budget",
            }

            rejected = await client.call_tool(
                "flow.run",
                {
                    "plan_identity": plan_identity,
                    "budget": {"maximum_seconds": 30, "maximum_nodes": 1},
                    "command": "touch owned",
                },
            )
            assert rejected.is_error is True
            assert not (tmp_path / "owned").exists()

            submitted = await client.call_tool(
                "flow.run",
                {
                    "plan_identity": plan_identity,
                    "budget": {"maximum_seconds": 30, "maximum_nodes": 1},
                },
            )
            assert submitted.is_error is False
            run_id = submitted.structured_content["data"]["management"]["run_id"]
            for _attempt in range(200):
                inspected = await client.call_tool(
                    "run.inspect",
                    {
                        "owner": "example",
                        "flow": "pipeline",
                        "target": "all",
                        "run_id": run_id,
                    },
                )
                if inspected.structured_content["data"]["management"]["status"] == "accepted":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("MCP-managed fake Flow did not finish")
            assert inspected.structured_content == execution.inspect_run(run_id=run_id)
            assert execution.run_flow(
                plan_identity=plan_identity,
                budget=AgenticExecutionBudget(30, 1),
                wait=True,
            )["data"]["result"] == inspected.structured_content["data"]["result"]

    asyncio.run(scenario())


def test_native_mcp_rejects_cross_project_read_execution_composition(
    tmp_path: Path,
) -> None:
    read_root = tmp_path / "read-project"
    execution_root = tmp_path / "execution-project"
    write_project_context(read_root)
    write_project_context(execution_root)
    write_mcp_project(read_root)
    write_mcp_project(execution_root)
    read = AgenticReadInterface.from_project_root(read_root)
    execution_read = AgenticReadInterface.from_project_root(execution_root)
    plan = execution_read.plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile="offline",
    )
    execution = AgenticExecutionInterface(
        execution_read,
        grant=AgenticExecutionGrant(
            principal="test-operator",
            role="design-operator",
            capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
            approved_plans=(
                AgenticPlanApproval(
                    plan["data"]["plan_identity"],
                    canonical_json(plan["data"]["plan"]),
                ),
            ),
            approval="cross-project-rejection",
            expires_at="2099-01-01T00:00:00+00:00",
        ),
    )

    with pytest.raises(TypeError):
        create_server(read, execution=execution)  # type: ignore[call-arg]


def test_native_mcp_campaign_plan_and_run_match_shared_interfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    original = agentic_read_module.builtin_workflow_registry
    monkeypatch.setattr(
        agentic_read_module,
        "builtin_workflow_registry",
        lambda owner_root: campaign_registry(original, owner_root),
    )
    source = _campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    expected_plan = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = expected_plan["data"]["campaign_identity"]
    execution = AgenticExecutionInterface(
        read,
        grant=campaign_grant(campaign_identity, expected_plan["data"]["plan"]),
    )
    server = create_server(execution)

    async def scenario() -> None:
        async with Client(server) as client:
            planned = await client.call_tool(
                "campaign.plan",
                {"campaign": source.canonical_json()},
            )
            assert planned.is_error is False
            assert planned.structured_content == expected_plan

            executed = await client.call_tool(
                "campaign.run",
                {
                    "campaign": source.canonical_json(),
                    "campaign_identity": campaign_identity,
                },
            )
            assert executed.is_error is False
            assert executed.structured_content == execution.run_campaign(
                campaign_json=source.canonical_json(),
                campaign_identity=campaign_identity,
            )
            assert executed.structured_content["conclusion"] == "passed"

            rejected = await client.call_tool(
                "campaign.run",
                {
                    "campaign": source.canonical_json(),
                    "campaign_identity": campaign_identity,
                    "command": "touch owned",
                },
            )
            assert rejected.is_error is True
            assert not (tmp_path / "owned").exists()

    asyncio.run(scenario())


def test_native_mcp_campaign_run_resumes_with_semantic_proposal_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_campaign_project(tmp_path)
    original = agentic_read_module.builtin_workflow_registry
    monkeypatch.setattr(
        agentic_read_module,
        "builtin_workflow_registry",
        lambda owner_root: _feedback_registry(original, owner_root),
    )
    source = _feedback_campaign()
    read = AgenticReadInterface.from_project_root(tmp_path)
    planned = read.plan_campaign(campaign_json=source.canonical_json())
    campaign_identity = planned["data"]["campaign_identity"]
    execution = AgenticExecutionInterface(
        read,
        grant=campaign_grant(campaign_identity, planned["data"]["plan"]),
    )
    server = create_server(execution)

    async def scenario() -> None:
        async with Client(server) as client:
            started = await client.call_tool(
                "campaign.run",
                {
                    "campaign": source.canonical_json(),
                    "campaign_identity": campaign_identity,
                },
            )
            assert started.is_error is False
            assert started.structured_content["conclusion"] == "proposal_required"
            state = design_campaign_state_from_json(
                canonical_json(started.structured_content["data"]["state"])
            )
            assert state.attribution is not None
            proposal = DesignRepairProposal(
                "example:proposal:native-mcp-round-2",
                "example",
                campaign_identity,
                state.iterations[-1].candidate.identity,
                state.attribution.identity,
                tuple(item.identity for item in state.attribution.evidence),
                _topology(master="BUF", origin=TopologyOrigin.PROPOSED),
                None,
                None,
                ("l0-functional",),
                ProposalProvenance(
                    "test-semantic-client",
                    "1",
                    (state.iterations[-1].candidate.identity,),
                ),
            )
            completed = await client.call_tool(
                "campaign.run",
                {
                    "run_id": started.structured_content["data"]["management"]["run_id"],
                    "proposal": proposal.canonical_json(),
                },
            )
            assert completed.is_error is False
            assert completed.structured_content["conclusion"] == "passed"
            assert len(completed.structured_content["data"]["state"]["iterations"]) == 2

    asyncio.run(scenario())


def test_native_mcp_stdio_executes_only_the_launcher_grant(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    read = AgenticReadInterface.from_project_root(tmp_path)
    plan = read.plan_flow(
        owner="example",
        flow="pipeline",
        target="all",
        profile="offline",
    )
    plan_identity = plan["data"]["plan_identity"]
    grant = AgenticExecutionGrant(
        principal="stdio-operator",
        role="design-operator",
        capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
        approved_plans=(
            AgenticPlanApproval(plan_identity, canonical_json(plan["data"]["plan"])),
        ),
        approval="stdio-execution-test",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    grant_path = tmp_path / "stdio-grant.json"
    grant_path.write_text(grant.canonical_json(), encoding="utf-8")
    python_path = os.environ.get("PYTHONPATH", "")

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "sigilicon.integrations.mcp.stdio",
                "--project-root",
                str(tmp_path),
                "--execution-grant",
                str(grant_path),
            ],
            env={"PYTHONPATH": python_path},
        )
        async with Client(parameters) as client:
            tools = await client.list_tools()
            assert "flow.run" in {item.name for item in tools.tools}
            submitted = await client.call_tool(
                "flow.run",
                {
                    "plan_identity": plan_identity,
                    "budget": {"maximum_seconds": 30, "maximum_nodes": 1},
                },
            )
            assert submitted.is_error is False
            run_id = submitted.structured_content["data"]["management"]["run_id"]
            for _attempt in range(200):
                inspected = await client.call_tool(
                    "run.inspect",
                    {
                        "owner": "example",
                        "flow": "pipeline",
                        "target": "all",
                        "run_id": run_id,
                    },
                )
                if inspected.structured_content["data"]["management"]["status"] == "accepted":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("stdio-managed fake Flow did not finish")
            assert inspected.structured_content["data"]["result"]["status"] == "accepted"

    asyncio.run(scenario())
