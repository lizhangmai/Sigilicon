from __future__ import annotations

from types import SimpleNamespace

from sigilicon.virtuoso.oa import (
    validate_instance_parameters,
)


class RecordingClient:
    def __init__(self) -> None:
        self.sources: list[str] = []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute_skill(self, source: str, **_kwargs):
        self.sources.append(source)
        self.calls.append((source, dict(_kwargs)))
        if 'sprintf(nil "T|%s|%s|%s' in source:
            rows = []
            for view in ("schematic", "symbol"):
                rows.extend((f'T|{view}|IN|input', f'T|{view}|OUT|output'))
            return SimpleNamespace(output="\n".join(rows), errors=[])
        if 'sprintf(nil "P|%s|%s|%s' in source:
            return SimpleNamespace(
                output=(
                    'I|PIN0|basic|ipin\n'
                    'I|PIN1|basic|opin\n'
                    'I|PIN2|basic|iopin\n'
                    'I|X0|lib|CHILD\n'
                    'P|X0|lch|pPar("parent_l")\n'
                    'P|X0|w|1e-07'
                ),
                errors=[],
            )
        return SimpleNamespace(output="t", errors=[])


def test_instance_parameter_validation_reads_parent_oa_properties(
    workspace_factory,
) -> None:
    client = RecordingClient()
    with workspace_factory(client, library="lib") as operation:
        report = validate_instance_parameters(
            client,
            "lib",
            "PARENT",
            {"X0": ("CHILD", {"lch": "parent_l", "w": "100n"})},
            operation=operation,
        )

    assert report == {"passed": True, "instances": 1, "parameters": 2}
