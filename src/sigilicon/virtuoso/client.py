"""Construction of the one supported virtuoso-bridge client."""

from __future__ import annotations

from typing import Any

from sigilicon.virtuoso.bridge import create_client


def get_client(resources: Any) -> Any:
    client = create_client(resources)
    if not client.test_connection():
        raise RuntimeError(
            "Virtuoso bridge rejected the explicit runtime endpoint; "
            "start the managed daemon and verify its host and port"
        )
    return client
