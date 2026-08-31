"""Strict read-only MCP projection of the client-neutral agentic Interface."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError

from sigilicon.workflows.agentic_read import AgenticReadInterface, READ_RESULT_KIND
from sigilicon.workflows.agentic_execution import (
    AgenticExecutionBudget,
    AgenticExecutionInterface,
)
from sigilicon.identifiers import RUN_ID_PATTERN


_OWNER_PATTERN = r"[A-Za-z][A-Za-z0-9_.-]*"
_IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*"

_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "schema": {"const": 1},
        "contract_kind": {"const": READ_RESULT_KIND},
        "operation": {"type": "string"},
        "project_id": {"type": "string", "minLength": 1},
        "authority": {"type": "string"},
        "conclusion": {"type": "string"},
        "summary": {"type": "string"},
        "data": {"type": "object"},
        "resources": {"type": "array", "items": {"type": "string"}},
        "allowed_next_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "schema",
        "contract_kind",
        "operation",
        "project_id",
        "authority",
        "conclusion",
        "summary",
        "data",
        "resources",
        "allowed_next_actions",
    ],
    "additionalProperties": False,
}

_PROJECT_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "owner": {"type": ["string", "null"], "pattern": f"^{_OWNER_PATTERN}$"},
    },
    "additionalProperties": False,
}

_TARGET_OPERATION_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "owner": {"type": "string", "pattern": f"^{_OWNER_PATTERN}$"},
        "target": {"type": "string", "pattern": f"^{_IDENTIFIER_PATTERN}$"},
        "operation": {"type": "string", "pattern": f"^{_IDENTIFIER_PATTERN}$"},
    },
    "required": ["owner", "target", "operation"],
    "additionalProperties": False,
}

_RUN_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "owner": {"type": "string", "pattern": f"^{_OWNER_PATTERN}$"},
        "target": {"type": "string", "pattern": f"^{_IDENTIFIER_PATTERN}$"},
        "operation": {"type": "string", "pattern": f"^{_IDENTIFIER_PATTERN}$"},
        "run_id": {"type": "string", "pattern": f"^{RUN_ID_PATTERN}$"},
    },
    "required": ["owner", "target", "operation", "run_id"],
    "additionalProperties": False,
}

_CANDIDATE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "owner": {"type": "string", "pattern": f"^{_OWNER_PATTERN}$"},
        "candidate": {"type": "string", "maxLength": 1_000_000},
        "artifacts": {
            "type": "array",
            "items": {"type": "string", "maxLength": 1_000_000},
            "minItems": 1,
            "maxItems": 128,
        },
    },
    "required": ["owner", "candidate", "artifacts"],
    "additionalProperties": False,
}

_PROMOTION_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "owner": {"type": "string", "pattern": f"^{_OWNER_PATTERN}$"},
        "candidate": {"type": "string", "maxLength": 1_000_000},
        "artifacts": {
            "type": "array",
            "items": {"type": "string", "maxLength": 1_000_000},
            "minItems": 1,
            "maxItems": 128,
        },
        "decision": {"type": "string", "maxLength": 1_000_000},
        "request": {"type": "string", "maxLength": 1_000_000},
    },
    "required": ["owner", "candidate", "artifacts", "decision", "request"],
    "additionalProperties": False,
}

_CAMPAIGN_PLAN_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "campaign": {"type": "string", "maxLength": 2_000_000},
    },
    "required": ["campaign"],
    "additionalProperties": False,
}

_TARGET_RUN_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "plan_identity": {"type": "string", "minLength": 1},
        "budget": {
            "type": "object",
            "properties": {
                "maximum_seconds": {"type": "integer", "minimum": 1, "maximum": 86_400},
                "maximum_nodes": {"type": "integer", "minimum": 1, "maximum": 10_000},
            },
            "required": ["maximum_seconds", "maximum_nodes"],
            "additionalProperties": False,
        },
    },
    "required": ["plan_identity", "budget"],
    "additionalProperties": False,
}

_RUN_CANCEL_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "run_id": {"type": "string", "pattern": f"^{RUN_ID_PATTERN}$"},
    },
    "required": ["run_id"],
    "additionalProperties": False,
}

_CAMPAIGN_RUN_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "campaign": {"type": "string", "maxLength": 2_000_000},
        "campaign_identity": {
            "type": "string",
            "minLength": 1,
        },
        "run_id": {"type": "string", "pattern": f"^{RUN_ID_PATTERN}$"},
        "proposal": {"type": "string", "maxLength": 2_000_000},
    },
    "oneOf": [
        {"required": ["campaign", "campaign_identity"]},
        {"required": ["run_id", "proposal"]},
    ],
    "additionalProperties": False,
}


class _RequestRejected(ValueError):
    pass


def _read_annotations() -> types.ToolAnnotations:
    return types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _tools(*, execution_enabled: bool) -> list[types.Tool]:
    annotations = _read_annotations()
    tools = [
        types.Tool(
            name="project.inspect",
            title="Inspect Sigilicon project",
            description=(
                "Validate the bound project's canonical owners and summarize "
                "source and owner targets. Runtime capabilities and product "
                "qualification remain not evaluated."
            ),
            inputSchema=_PROJECT_INPUT_SCHEMA,
            outputSchema=_RESPONSE_SCHEMA,
            annotations=annotations,
        ),
        types.Tool(
            name="target.plan",
            title="Plan Sigilicon target operation",
            description=(
                "Resolve one owner target and operation through FlowEngine without "
                "executing a backend or writing artifacts."
            ),
            inputSchema=_TARGET_OPERATION_INPUT_SCHEMA,
            outputSchema=_RESPONSE_SCHEMA,
            annotations=annotations,
        ),
        types.Tool(
            name="run.inspect",
            title="Inspect Sigilicon target-operation result",
            description=(
                "Read one identity-matched persisted target-operation result from the bound "
                "artifact root without adding qualification authority."
            ),
            inputSchema=_RUN_INPUT_SCHEMA,
            outputSchema=_RESPONSE_SCHEMA,
            annotations=annotations,
        ),
        types.Tool(
            name="candidate.validate",
            title="Validate Sigilicon Design Candidate",
            description=(
                "Validate exact canonical Candidate stage JSON, owner lineage, and "
                "content identities without reading a path or promoting source."
            ),
            inputSchema=_CANDIDATE_INPUT_SCHEMA,
            outputSchema=_RESPONSE_SCHEMA,
            annotations=annotations,
        ),
        types.Tool(
            name="campaign.plan",
            title="Plan bounded Sigilicon Design Campaign",
            description=(
                "Compile strict target-operation selectors, typed output bindings, budgets, "
                "repair lineage, and stop conditions without executing a backend."
            ),
            inputSchema=_CAMPAIGN_PLAN_INPUT_SCHEMA,
            outputSchema=_RESPONSE_SCHEMA,
            annotations=annotations,
        ),
        types.Tool(
            name="candidate.promotion_plan",
            title="Prepare Sigilicon Candidate Promotion Plan",
            description=(
                "Validate exact Candidate, Decision, and Evidence identities and compile "
                "an immutable human-review plan. This tool cannot write source or apply a patch."
            ),
            inputSchema=_PROMOTION_INPUT_SCHEMA,
            outputSchema=_RESPONSE_SCHEMA,
            annotations=annotations,
        ),
    ]
    if execution_enabled:
        execute_annotations = types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
        tools.extend(
            (
                types.Tool(
                    name="target.run",
                    title="Run approved Sigilicon Target Operation Plan",
                    description=(
                        "Submit one exact launcher-approved Plan with a bounded node/time "
                        "budget. No command, path, environment, or Adapter selector is accepted."
                    ),
                    inputSchema=_TARGET_RUN_INPUT_SCHEMA,
                    outputSchema=_RESPONSE_SCHEMA,
                    annotations=execute_annotations,
                ),
                types.Tool(
                    name="run.cancel",
                    title="Cancel managed Sigilicon Target Run",
                    description=(
                        "Cooperatively cancel one exact principal-bound managed run and "
                        "return its immutable terminal state."
                    ),
                    inputSchema=_RUN_CANCEL_INPUT_SCHEMA,
                    outputSchema=_RESPONSE_SCHEMA,
                    annotations=execute_annotations,
                ),
                types.Tool(
                    name="campaign.run",
                    title="Run approved bounded Sigilicon Design Campaign",
                    description=(
                        "Start one exact launcher-approved Campaign or resume its durable "
                        "run with one strict semantic proposal. No command, path, environment, "
                        "future attempt, result, or Adapter selector is accepted."
                    ),
                    inputSchema=_CAMPAIGN_RUN_INPUT_SCHEMA,
                    outputSchema=_RESPONSE_SCHEMA,
                    annotations=execute_annotations,
                ),
            )
        )
    return tools


def _strict_arguments(
    raw: dict[str, Any] | None,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, Any]:
    arguments = {} if raw is None else raw
    unknown = set(arguments) - allowed
    missing = required - set(arguments)
    if unknown or missing:
        raise _RequestRejected("tool arguments do not match the declared schema")
    return arguments


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise _RequestRejected("tool arguments do not match the declared schema")
    return value


def _optional_text(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is not None and not isinstance(value, str):
        raise _RequestRejected("tool arguments do not match the declared schema")
    return value


def _error_response(
    interface: AgenticReadInterface,
    *,
    operation: str,
    code: str,
    summary: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "contract_kind": READ_RESULT_KIND,
        "operation": operation,
        "project_id": interface.project_id,
        "authority": "none",
        "conclusion": "non-conclusion",
        "summary": summary,
        "data": {"error": {"code": code}},
        "resources": [interface.project_resource_uri],
        "allowed_next_actions": ["project.inspect"],
    }


def _tool_result(payload: dict[str, Any], *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=payload["summary"])],
        structuredContent=payload,
        isError=is_error,
    )


def _json_resource(uri: str, payload: dict[str, Any]) -> types.ReadResourceResult:
    return types.ReadResourceResult(
        contents=[
            types.TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            )
        ],
        ttlMs=0,
        cacheScope="private",
    )


def _resource_segments(uri: str) -> tuple[str, ...]:
    if not uri.startswith("sigilicon://") or any(mark in uri for mark in ("%", "?", "#", "\\")):
        raise MCPError(types.INVALID_PARAMS, "Unknown Sigilicon resource")
    return tuple(part for part in uri.removeprefix("sigilicon://").split("/") if part)


def create_server(
    application: AgenticReadInterface | AgenticExecutionInterface,
) -> Server[object]:
    """Create the standards-compliant protocol shell around one bound Interface."""

    if isinstance(application, AgenticExecutionInterface):
        execution: AgenticExecutionInterface | None = application
        interface = application.read
    elif isinstance(application, AgenticReadInterface):
        execution = None
        interface = application
    else:
        raise TypeError("MCP requires one Agentic application Interface")

    async def list_tools(
        _context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        if params is not None and params.cursor is not None:
            raise MCPError(types.INVALID_PARAMS, "Unknown tool-list cursor")
        return types.ListToolsResult(
            tools=_tools(execution_enabled=execution is not None),
            ttlMs=0,
            cacheScope="private",
        )

    async def call_tool(
        _context: ServerRequestContext[object],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        operation = params.name
        try:
            if operation == "project.inspect":
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"owner"}),
                    required=frozenset(),
                )
                payload = interface.inspect_project(
                    owner=_optional_text(arguments, "owner")
                )
            elif operation == "target.plan":
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"owner", "target", "operation"}),
                    required=frozenset({"owner", "target", "operation"}),
                )
                payload = interface.plan_target(
                    owner=_required_text(arguments, "owner"),
                    target=_required_text(arguments, "target"),
                    operation=_required_text(arguments, "operation"),
                )
            elif operation == "run.inspect":
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"owner", "target", "operation", "run_id"}),
                    required=frozenset({"owner", "target", "operation", "run_id"}),
                )
                payload = interface.inspect_run(
                    owner=_required_text(arguments, "owner"),
                    target=_required_text(arguments, "target"),
                    operation=_required_text(arguments, "operation"),
                    run_id=_required_text(arguments, "run_id"),
                )
            elif operation == "candidate.validate":
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"owner", "candidate", "artifacts"}),
                    required=frozenset({"owner", "candidate", "artifacts"}),
                )
                artifact_values = arguments.get("artifacts")
                if (
                    not isinstance(artifact_values, list)
                    or not artifact_values
                    or len(artifact_values) > 128
                    or any(
                        not isinstance(item, str) or len(item) > 1_000_000
                        for item in artifact_values
                    )
                ):
                    raise _RequestRejected(
                        "tool arguments do not match the declared schema"
                    )
                candidate_json = _required_text(arguments, "candidate")
                if len(candidate_json) > 1_000_000:
                    raise _RequestRejected(
                        "tool arguments do not match the declared schema"
                    )
                payload = interface.validate_candidate(
                    owner=_required_text(arguments, "owner"),
                    candidate_json=candidate_json,
                    artifact_json=tuple(artifact_values),
                )
            elif operation == "campaign.plan":
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"campaign"}),
                    required=frozenset({"campaign"}),
                )
                campaign_json = _required_text(arguments, "campaign")
                if len(campaign_json) > 2_000_000:
                    raise _RequestRejected(
                        "tool arguments do not match the declared schema"
                    )
                payload = interface.plan_campaign(campaign_json=campaign_json)
            elif operation == "candidate.promotion_plan":
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset(
                        {"owner", "candidate", "artifacts", "decision", "request"}
                    ),
                    required=frozenset(
                        {"owner", "candidate", "artifacts", "decision", "request"}
                    ),
                )
                artifact_values = arguments.get("artifacts")
                texts = (
                    arguments.get("candidate"),
                    arguments.get("decision"),
                    arguments.get("request"),
                )
                if (
                    not isinstance(artifact_values, list)
                    or not artifact_values
                    or len(artifact_values) > 128
                    or any(
                        not isinstance(item, str) or len(item) > 1_000_000
                        for item in (*artifact_values, *texts)
                    )
                ):
                    raise _RequestRejected(
                        "tool arguments do not match the declared schema"
                    )
                payload = interface.plan_candidate_promotion(
                    owner=_required_text(arguments, "owner"),
                    candidate_json=_required_text(arguments, "candidate"),
                    artifact_json=tuple(artifact_values),
                    decision_json=_required_text(arguments, "decision"),
                    request_json=_required_text(arguments, "request"),
                )
            elif operation == "target.run" and execution is not None:
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"plan_identity", "budget"}),
                    required=frozenset({"plan_identity", "budget"}),
                )
                budget_value = arguments.get("budget")
                if not isinstance(budget_value, dict) or set(budget_value) != {
                    "maximum_seconds",
                    "maximum_nodes",
                }:
                    raise _RequestRejected(
                        "tool arguments do not match the declared schema"
                    )
                budget = AgenticExecutionBudget(
                    maximum_seconds=budget_value.get("maximum_seconds"),
                    maximum_nodes=budget_value.get("maximum_nodes"),
                )
                payload = execution.run_target(
                    plan_identity=_required_text(arguments, "plan_identity"),
                    budget=budget,
                    wait=False,
                )
            elif operation == "run.cancel" and execution is not None:
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset({"run_id"}),
                    required=frozenset({"run_id"}),
                )
                payload = execution.cancel_run(
                    run_id=_required_text(arguments, "run_id")
                )
            elif operation == "campaign.run" and execution is not None:
                arguments = _strict_arguments(
                    params.arguments,
                    allowed=frozenset(
                        {"campaign", "campaign_identity", "run_id", "proposal"}
                    ),
                    required=frozenset(),
                )
                start_fields = {"campaign", "campaign_identity"}
                resume_fields = {"run_id", "proposal"}
                argument_fields = frozenset(arguments)
                if argument_fields not in {
                    frozenset(start_fields),
                    frozenset(resume_fields),
                }:
                    raise _RequestRejected(
                        "tool arguments do not match the declared schema"
                    )
                if argument_fields == start_fields:
                    campaign_json = _required_text(arguments, "campaign")
                    if len(campaign_json) > 2_000_000:
                        raise _RequestRejected(
                            "tool arguments do not match the declared schema"
                        )
                    payload = execution.run_campaign(
                        campaign_json=campaign_json,
                        campaign_identity=_required_text(
                            arguments,
                            "campaign_identity",
                        ),
                    )
                else:
                    proposal_json = _required_text(arguments, "proposal")
                    if len(proposal_json) > 2_000_000:
                        raise _RequestRejected(
                            "tool arguments do not match the declared schema"
                        )
                    payload = execution.run_campaign(
                        run_id=_required_text(arguments, "run_id"),
                        proposal_json=proposal_json,
                    )
            else:
                return _tool_result(
                    _error_response(
                        interface,
                        operation="unknown",
                        code="unknown-tool",
                        summary="The requested tool is not registered by this server.",
                    ),
                    is_error=True,
                )
        except _RequestRejected:
            return _tool_result(
                _error_response(
                    interface,
                    operation=operation,
                    code="invalid-arguments",
                    summary="The request does not match the tool's strict input schema.",
                ),
                is_error=True,
            )
        except (OSError, RuntimeError, ValueError):
            return _tool_result(
                _error_response(
                    interface,
                    operation=operation,
                    code="contract-rejected",
                    summary=(
                        "The bound project contract rejected the requested identity; "
                        "no action was performed."
                    ),
                ),
                is_error=True,
            )
        return _tool_result(payload, is_error=False)

    async def list_resources(
        _context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        if params is not None and params.cursor is not None:
            raise MCPError(types.INVALID_PARAMS, "Unknown resource-list cursor")
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    name="bound-project",
                    title="Bound Sigilicon project",
                    uri=interface.project_resource_uri,
                    description="Canonical owner, target, and source-status projection.",
                    mimeType="application/json",
                )
            ],
            ttlMs=0,
            cacheScope="private",
        )

    async def list_resource_templates(
        _context: ServerRequestContext[object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourceTemplatesResult:
        if params is not None and params.cursor is not None:
            raise MCPError(types.INVALID_PARAMS, "Unknown resource-template cursor")
        return types.ListResourceTemplatesResult(
            resourceTemplates=[
                types.ResourceTemplate(
                    name="owner-targets",
                    title="Owner target projection",
                    uriTemplate="sigilicon://owners/{owner}/targets",
                    description="One exact owner and its target operations from the bound project.",
                    mimeType="application/json",
                ),
                types.ResourceTemplate(
                    name="target-operation-run-result",
                    title="Persisted target-operation result",
                    uriTemplate=(
                        "sigilicon://runs/{owner}/{target}/{operation}/{run_id}/manifest"
                    ),
                    description="One identity-matched result in the bound artifact root.",
                    mimeType="application/json",
                ),
            ],
            ttlMs=0,
            cacheScope="private",
        )

    async def read_resource(
        _context: ServerRequestContext[object],
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        uri = str(params.uri)
        if uri == interface.project_resource_uri:
            return _json_resource(uri, interface.inspect_project(owner=None))
        segments = _resource_segments(uri)
        try:
            if len(segments) == 3 and segments[0] == "owners" and segments[2] == "targets":
                payload = interface.inspect_project(owner=segments[1])
            elif (
                len(segments) == 6
                and segments[0] == "runs"
                and segments[5] == "manifest"
            ):
                payload = interface.inspect_run(
                    owner=segments[1],
                    target=segments[2],
                    operation=segments[3],
                    run_id=segments[4],
                )
            else:
                raise MCPError(types.INVALID_PARAMS, "Unknown Sigilicon resource")
        except MCPError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise MCPError(
                types.INVALID_PARAMS,
                "The bound project contract rejected this resource identity",
            ) from exc
        return _json_resource(uri, payload)

    return Server(
        "sigilicon-native",
        version="0.1.0",
        title="Sigilicon Native MCP Server",
        description=(
            "Project inspection, deterministic Flow planning, and optional "
            "launcher-authorized execution through Sigilicon-owned Interfaces."
            if execution is not None
            else "Read-only project inspection and deterministic Flow planning "
            "through Sigilicon-owned Interfaces."
        ),
        instructions=(
            "Treat source-contract validation, plans, and recorded Flow results as "
            "distinct authority levels. Never infer DRC, LVS, PEX, qualification, "
            "or signoff beyond an exact recorded result."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_resource_templates,
        on_read_resource=read_resource,
    )


__all__ = ["create_server"]
