from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

mcp = pytest.importorskip("mcp")

from conftest import (
    write_component_owner,
    write_fake_action_module,
    write_project_context,
)
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
from sigilicon.domain.repository import Project
from sigilicon.workflows.agentic_execution import AgenticExecutionInterface
from sigilicon.workflows.agentic_read import AgenticReadInterface
from sigilicon.workflows.project import bind_run_read
from test_design_promotion import _inputs as promotion_inputs


def _read(root: Path) -> AgenticReadInterface:
    return AgenticReadInterface.from_project(Project.from_project_root(root))


def write_mcp_project(root: Path) -> None:
    write_project_context(root)
    owner_root = root / "ip/example"
    flow_root = owner_root / "configs/flows"
    flow_root.mkdir(parents=True)
    (flow_root / "pipeline.toml").write_text(
        '''schema = 1
contract_kind = "execution-recipe"
path_scope = "owner"
owner = "example"
name = "pipeline"

[actions."fake.source"]
adapter = "fake-source"

[[nodes]]
id = "source"
action = "fake.source"
config = { text = "hello" }
''',
        encoding="utf-8",
    )
    catalog = owner_root / "configs/targets.toml"
    catalog.write_text(
        '''schema = 1
contract_kind = "owner-targets"
path_scope = "owner"
owner = "example"

[targets.pipeline]
description = "MCP pipeline"

[targets.pipeline.operations.all]
recipe = "configs/flows/pipeline.toml"
goals = ["source"]
''',
        encoding="utf-8",
    )
    extension = write_fake_action_module(root, "example")
    write_component_owner(
        root,
        "example",
        filesets={
            "flow": tuple(
                path.relative_to(root).as_posix()
                for path in (
                    catalog,
                    flow_root / "pipeline.toml",
                    extension,
                )
            )
        },
    )
    component_path = owner_root / "component.toml"
    component_path.write_text(
        component_path.read_text(encoding="utf-8").replace(
            "\n[filesets]\n",
            '\ntarget_catalog = "ip/example/configs/targets.toml"\n\n[filesets]\n',
        ),
        encoding="utf-8",
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
    interface = _read(tmp_path)
    server = create_server(interface)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "project.inspect",
                "target.plan",
                "run.inspect",
                "candidate.validate",
                "candidate.promotion_plan",
            ]
            assert all(
                tool.annotations is not None
                and tool.annotations.read_only_hint is True
                and tool.annotations.open_world_hint is False
                and tool.input_schema.get("additionalProperties") is False
                for tool in listed.tools
            )
            flow_plan_properties = set(
                next(tool for tool in listed.tools if tool.name == "target.plan")
                .input_schema["properties"]
            )
            assert flow_plan_properties == {
                "owner",
                "target",
                "operation",
            }
            assert "flow" not in flow_plan_properties
            assert "profile" not in flow_plan_properties

            expected = interface.plan_target(
                owner="example",
                target="pipeline",
                operation="all",
            )
            result = await client.call_tool(
                "target.plan",
                {"owner": "example", "target": "pipeline", "operation": "all"},
            )
            assert result.is_error is False
            assert result.structured_content == expected

            old_selectors = await client.call_tool(
                "target.plan",
                {
                    "owner": "example",
                    "flow": "pipeline",
                    "target": "all",
                    "profile": "offline",
                },
            )
            assert old_selectors.is_error is True

            unknown = await client.call_tool(
                "project.inspect",
                {"owner": "example", "unexpected": "ignored?"},
            )
            assert unknown.is_error is True
            assert unknown.structured_content["conclusion"] == "non-conclusion"

            injected = await client.call_tool(
                "target.plan",
                {
                    "owner": "example",
                    "target": "pipeline; touch owned",
                    "operation": "all",
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

            campaign = await client.call_tool(
                "campaign.plan",
                {"campaign": "{}"},
            )
            assert campaign.is_error is True
            assert campaign.structured_content["data"]["error"]["code"] == "unknown-tool"

            resources = await client.list_resources()
            assert [str(item.uri) for item in resources.resources] == [
                interface.project_resource_uri
            ]
            resource = await client.read_resource(interface.project_resource_uri)
            payload = json.loads(resource.contents[0].text)
            assert payload == interface.inspect_project(owner=None)

            templates = await client.list_resource_templates()
            assert [item.name for item in templates.resource_templates] == [
                "owner-targets",
                "target-operation-run-result",
            ]
            with pytest.raises(MCPError):
                await client.read_resource("sigilicon://owners/../targets")

    asyncio.run(scenario())


def test_native_mcp_stdio_entrypoint_round_trips(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    expected = _read(tmp_path).inspect_project(
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
    read = _read(tmp_path)
    plan = read.plan_target(
        owner="example",
        target="pipeline",
        operation="all",
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
                "target.plan",
                "target.run",
                "run.inspect",
                "run.cancel",
                "candidate.validate",
                "candidate.promotion_plan",
            }
            assert tools["target.run"].annotations.read_only_hint is False
            assert tools["target.run"].annotations.destructive_hint is True
            assert tools["run.cancel"].annotations.read_only_hint is False
            assert tools["run.cancel"].annotations.destructive_hint is True
            run_id_patterns = {
                tools["run.inspect"].input_schema["properties"]["run_id"]["pattern"],
                tools["run.cancel"].input_schema["properties"]["run_id"]["pattern"],
            }
            assert len(run_id_patterns) == 1
            assert set(tools["target.run"].input_schema["properties"]) == {
                "plan_identity",
                "budget",
            }
            assert set(tools["target.plan"].input_schema["properties"]) == {
                "owner",
                "target",
                "operation",
            }
            assert set(tools["run.inspect"].input_schema["properties"]) == {
                "owner",
                "target",
                "operation",
                "run_id",
            }
            assert "flow" not in tools["run.inspect"].input_schema["properties"]
            assert "profile" not in tools["run.inspect"].input_schema["properties"]

            rejected = await client.call_tool(
                "target.run",
                {
                    "plan_identity": plan_identity,
                    "budget": {"maximum_seconds": 30, "maximum_nodes": 1},
                    "command": "touch owned",
                },
            )
            assert rejected.is_error is True
            assert not (tmp_path / "owned").exists()

            submitted = await client.call_tool(
                "target.run",
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
                        "target": "pipeline",
                        "operation": "all",
                        "run_id": run_id,
                    },
                )
                if inspected.structured_content["data"]["management"]["status"] == "accepted":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("MCP-managed fake Flow did not finish")
            assert inspected.structured_content == execution.inspect_run(run_id=run_id)
            run_uri = inspected.structured_content["resources"][0]
            resource = await client.read_resource(run_uri)
            assert json.loads(resource.contents[0].text) == inspected.structured_content
            with pytest.raises(MCPError):
                await client.read_resource(
                    f"sigilicon://runs/example/pipeline/all/{run_id}/manifest"
                )
            assert execution.run_target(
                plan_identity=plan_identity,
                budget=AgenticExecutionBudget(30, 1),
                wait=True,
            )["data"]["result"] == inspected.structured_content["data"]["result"]

    asyncio.run(scenario())


def test_native_mcp_history_only_does_not_load_current_catalogs(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    read = _read(tmp_path)
    plan = read.plan_target(owner="example", target="pipeline", operation="all")
    plan_identity = plan["data"]["plan_identity"]
    execution = AgenticExecutionInterface(
        read,
        grant=AgenticExecutionGrant(
            principal="history-operator",
            role="design-operator",
            capabilities=(AgenticExecutionCapability.EXECUTE_DERIVED,),
            approved_plans=(
                AgenticPlanApproval(
                    plan_identity,
                    canonical_json(plan["data"]["plan"]),
                ),
            ),
            approval="history-only-test",
            expires_at="2099-01-01T00:00:00+00:00",
        ),
    )
    completed = execution.run_target(
        plan_identity=plan_identity,
        budget=AgenticExecutionBudget(30, 1),
        wait=True,
    )
    run_id = completed["data"]["management"]["run_id"]
    (tmp_path / "ip/example/configs/flows/pipeline.toml").unlink()
    (tmp_path / "ip/example/configs/targets.toml").unlink()
    server = create_server(bind_run_read(tmp_path))

    async def scenario() -> None:
        async with Client(server) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["run.inspect"]
            inspected = await client.call_tool(
                "run.inspect",
                {
                    "owner": "example",
                    "target": "pipeline",
                    "operation": "all",
                    "run_id": run_id,
                },
            )
            assert inspected.structured_content["data"] == completed["data"]
            templates = await client.list_resource_templates()
            assert [item.name for item in templates.resource_templates] == [
                "target-operation-run-result"
            ]

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
    read = _read(read_root)
    execution_read = _read(execution_root)
    plan = execution_read.plan_target(
        owner="example",
        target="pipeline",
        operation="all",
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


def test_native_mcp_stdio_executes_only_the_launcher_grant(tmp_path: Path) -> None:
    write_mcp_project(tmp_path)
    read = _read(tmp_path)
    plan = read.plan_target(
        owner="example",
        target="pipeline",
        operation="all",
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
            assert "target.run" in {item.name for item in tools.tools}
            submitted = await client.call_tool(
                "target.run",
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
                        "target": "pipeline",
                        "operation": "all",
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
