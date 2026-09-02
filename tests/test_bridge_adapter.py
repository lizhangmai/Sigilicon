from __future__ import annotations

import pytest

from sigilicon.execution import ContractError
from sigilicon.execution.model import Resources
from sigilicon.virtuoso import bridge
from sigilicon.virtuoso import client as client_adapter


def test_missing_bridge_dependency_has_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = ModuleNotFoundError("No module named 'virtuoso_bridge'")
    missing.name = "virtuoso_bridge"
    monkeypatch.setattr(
        bridge,
        "import_module",
        lambda _name: (_ for _ in ()).throw(missing),
    )

    with pytest.raises(bridge.BridgeDependencyUnavailable, match="'virtuoso' extra"):
        bridge.create_client(
            Resources(
                values={
                    "virtuoso-bridge.host": "127.0.0.1",
                    "virtuoso-bridge.port": "65432",
                }
            )
        )


def test_direct_client_uses_only_the_explicit_resources_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("constructor", kwargs))

        @classmethod
        def from_env(cls) -> "FakeClient":
            raise AssertionError("environment factory must not be called")

    monkeypatch.setattr(
        bridge,
        "_bridge_attribute",
        lambda module, name: (
            calls.append(("lookup", (module, name))) or FakeClient
        ),
    )
    resources = Resources(
        values={
            "virtuoso-bridge.host": "127.0.0.1",
            "virtuoso-bridge.port": "65432",
        },
        environment={
            "VB_REMOTE_HOST": "must-not-be-read",
            "VB_REMOTE_PORT": "must-not-be-read",
        }
    )

    bridge.create_client(resources)

    assert calls == [
        ("lookup", ("virtuoso_bridge", "VirtuosoClient")),
        ("constructor", {"host": "127.0.0.1", "port": 65432}),
    ]


@pytest.mark.parametrize(
    "environment",
    [
        {"virtuoso-bridge.port": "65432"},
        {"virtuoso-bridge.host": "127.0.0.1"},
        {
            "virtuoso-bridge.host": "   ",
            "virtuoso-bridge.port": "65432",
        },
        {
            "virtuoso-bridge.host": "127.0.0.1",
            "virtuoso-bridge.port": "not-a-port",
        },
        {"virtuoso-bridge.host": "127.0.0.1", "virtuoso-bridge.port": "0"},
        {
            "virtuoso-bridge.host": "127.0.0.1",
            "virtuoso-bridge.port": "65536",
        },
    ],
)
def test_invalid_endpoint_fails_closed_before_loading_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    monkeypatch.setattr(
        bridge,
        "_bridge_attribute",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("bridge dependency must not load for an invalid endpoint")
        ),
    )

    with pytest.raises((ContractError, ValueError)):
        bridge.create_client(Resources(values=environment))


def test_get_client_requires_resources_and_forwards_the_same_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = Resources(
        values={
            "virtuoso-bridge.host": "127.0.0.1",
            "virtuoso-bridge.port": "65432",
        }
    )
    received: list[object] = []

    class ConnectedClient:
        def test_connection(self) -> bool:
            return True

    monkeypatch.setattr(
        client_adapter,
        "create_client",
        lambda value: received.append(value) or ConnectedClient(),
    )

    assert client_adapter.get_client(resources).__class__ is ConnectedClient
    assert received == [resources]
    with pytest.raises(TypeError):
        client_adapter.get_client()  # type: ignore[call-arg]
