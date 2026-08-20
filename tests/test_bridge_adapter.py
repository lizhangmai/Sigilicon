from __future__ import annotations

import pytest

from sigilicon.virtuoso import bridge


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
        bridge.create_client_from_env()
