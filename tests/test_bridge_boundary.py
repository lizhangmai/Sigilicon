from __future__ import annotations

from types import SimpleNamespace

import pytest

from sigilicon.virtuoso.discovery import list_cells, list_libraries
from sigilicon.virtuoso.layout import _protected_layout_skill
from sigilicon.virtuoso.schematic import read_schematic


class RecordingClient:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.sources: list[str] = []

    def execute_skill(self, source: str, **_kwargs):
        self.sources.append(source)
        return SimpleNamespace(output=next(self.responses), errors=[], metadata={})


def test_library_discovery_uses_public_bridge_library_api() -> None:
    calls: list[int] = []
    client = SimpleNamespace(
        library=SimpleNamespace(
            list=lambda *, timeout: calls.append(timeout) or ["basic", "designLib"]
        )
    )

    assert list_libraries(client) == {"libraries": ["basic", "designLib"]}
    assert calls == [20]


def test_readonly_discovery_escapes_skill_strings() -> None:
    client = RecordingClient(('"lib"', '"cell|schematic\\n"'))

    data = list_cells(client, 'lib"unsafe')

    assert data["cells"] == [{"name": "cell", "views": ["schematic"]}]
    assert all('lib\\"unsafe' in source for source in client.sources)


def test_readonly_schematic_and_layout_sources_close_their_handles(
    workspace_factory,
) -> None:
    client = RecordingClient(("ERROR",))

    with workspace_factory(client, library='lib"unsafe') as operation:
        with pytest.raises(RuntimeError, match="cannot read schematic"):
            read_schematic(
                client,
                'lib"unsafe',
                "cell",
                include_positions=False,
                operation=operation,
            )

    schematic_source = client.sources[0]
    layout_source = _protected_layout_skill('lib"unsafe', "cell", "layout")
    for source in (schematic_source, layout_source):
        assert "unwindProtect" in source
        assert "dbClose" in source
        assert 'lib\\"unsafe' in source
    assert "preserved exact dbIds" in schematic_source
