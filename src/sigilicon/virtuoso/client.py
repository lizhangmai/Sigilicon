"""Construction of the one supported virtuoso-bridge client."""

from __future__ import annotations

from typing import Any

from sigilicon.virtuoso.bridge import (
    BridgeDependencyUnavailable,
    create_client_from_env,
)


def get_client() -> Any:
    try:
        client = create_client_from_env()
    except BridgeDependencyUnavailable:
        raise
    except Exception as exc:
        raise RuntimeError(
            "无法建立 virtuoso-bridge 连接；请先检查 "
            "pixi run virtuoso-bridge status"
        ) from exc
    if not client.test_connection():
        raise RuntimeError(
            "连不上 Virtuoso daemon（Virtuoso 是否已启动并加载 bridge？）；"
            "请检查 pixi run virtuoso-bridge status"
        )
    return client
