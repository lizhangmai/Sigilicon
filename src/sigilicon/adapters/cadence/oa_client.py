"""Bind a Virtuoso bridge client to an exact execution resource snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sigilicon.execution._model import ResourceBinding, Resources
from sigilicon.virtuoso.bridge import (
    VIRTUOSO_BRIDGE_HOST,
    VIRTUOSO_BRIDGE_PORT,
    create_client,
)


@dataclass(frozen=True)
class OaClient:
    raw: Any
    endpoint: tuple[ResourceBinding, ResourceBinding]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def require_resources(self, resources: Resources) -> None:
        if any(not resources.matches(binding) for binding in self.endpoint):
            raise RuntimeError(
                "Virtuoso bridge client belongs to a different runtime endpoint"
            )


def bind_client(client: Any, resources: Resources) -> OaClient:
    if isinstance(client, OaClient):
        client.require_resources(resources)
        return client
    return OaClient(
        client,
        (
            resources.capture(VIRTUOSO_BRIDGE_HOST),
            resources.capture(VIRTUOSO_BRIDGE_PORT),
        ),
    )


def get_client(resources: Resources) -> OaClient:
    raw = create_client(resources)
    if not raw.test_connection():
        raise RuntimeError(
            "Virtuoso bridge rejected the explicit runtime endpoint; "
            "start the managed daemon and verify its host and port"
        )
    return bind_client(raw, resources)


__all__ = ["OaClient", "bind_client", "get_client"]
