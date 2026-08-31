"""Public response projection shared by agent-facing read interfaces."""

from __future__ import annotations

import re
from typing import Any

from sigilicon.paths import ProjectContext


READ_RESULT_KIND = "agentic-read-result"
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s\"'<>]+)")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>]+")
_SENSITIVE_FIELDS = frozenset(
    {"command", "commands", "env", "environment", "executable", "raw_log"}
)


def repository_identity(manifest_owner: str, context: ProjectContext) -> str:
    return f"{manifest_owner}.{context.project_root.name}"


def public_value(value: Any, *, field: str | None = None) -> Any:
    """Bound untrusted records and remove site-private execution material."""

    if field in _SENSITIVE_FIELDS and not (
        field == "executable" and isinstance(value, bool)
    ):
        return "<redacted-private-execution-material>"
    if isinstance(value, dict):
        return {
            str(key): public_value(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        if len(value) > 4000:
            return "<redacted-oversized-text>"
        return _WINDOWS_PATH.sub(
            "<redacted-site-path>",
            _ABSOLUTE_PATH.sub("<redacted-site-path>", value),
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("record contains a non-portable public value")


def read_response(
    *,
    project_id: str,
    operation: str,
    authority: str,
    conclusion: str,
    summary: str,
    data: dict[str, Any],
    resources: list[str],
    allowed_next_actions: list[str],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "contract_kind": READ_RESULT_KIND,
        "operation": operation,
        "project_id": project_id,
        "authority": authority,
        "conclusion": conclusion,
        "summary": summary,
        "data": public_value(data),
        "resources": resources,
        "allowed_next_actions": allowed_next_actions,
    }


__all__ = [
    "READ_RESULT_KIND",
    "public_value",
    "read_response",
    "repository_identity",
]
