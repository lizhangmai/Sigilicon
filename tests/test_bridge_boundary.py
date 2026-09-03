from __future__ import annotations

from types import SimpleNamespace

import pytest

from sigilicon.virtuoso.discovery import list_cells, list_libraries
from sigilicon.virtuoso.oa import cell_exists, cell_view_exists
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


@pytest.mark.parametrize(
    ("query", "arguments"),
    (
        (cell_exists, ("lib", "cell")),
        (cell_view_exists, ("lib", "cell", "layout")),
    ),
)
@pytest.mark.parametrize(("output", "expected"), (("t", True), ("nil", False)))
def test_oa_existence_queries_return_only_skill_booleans(
    query,
    arguments,
    output: str,
    expected: bool,
) -> None:
    client = RecordingClient((output,))

    assert query(client, *arguments) is expected

    source = client.sources[0]
    assert source.startswith("if(ddGetObj(")
    assert source.endswith(" t nil)")


@pytest.mark.parametrize("query", (cell_exists, cell_view_exists))
def test_oa_existence_queries_reject_native_handle_results(query) -> None:
    client = RecordingClient(("dd:0x123",))
    arguments = ("lib", "cell") if query is cell_exists else ("lib", "cell", "layout")

    with pytest.raises(RuntimeError, match="invalid SKILL boolean"):
        query(client, *arguments)


def test_readonly_schematic_source_closes_its_handle(
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
    assert "unwindProtect" in schematic_source
    assert "dbClose" in schematic_source
    assert 'lib\\"unsafe' in schematic_source
    assert "preserved exact dbIds" in schematic_source
