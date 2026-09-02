from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.execution.model import Resources
from sigilicon.external_tools import ProcessResult
from sigilicon.virtuoso.text_view import import_oa_text_view
from sigilicon.domain.systemverilog import module_port_signatures


def test_systemverilog_port_parser_elaborates_constant_ternary_parameters() -> None:
    source = """
module engine #(
    parameter int SLOTS = 4,
    parameter int SLOT_BITS = SLOTS <= 1 ? 1 : $clog2(SLOTS)
) (
    input logic [SLOT_BITS-1:0] slot_i,
    output logic [SLOTS-1:0] slots_o
);
endmodule
"""

    ports = module_port_signatures(source, "engine")

    assert ports["slot_i"].width == 2
    assert ports["slots_o"].width == 4


@pytest.mark.parametrize(
    ("suffix", "kind", "language", "view"),
    (
        ("sv", "system_verilog", "systemverilog", "systemVerilog"),
        ("scs", "spectre_model", "spectre", "spectre"),
        ("va", "veriloga", "verilogams", "veriloga"),
    ),
)
def test_source_owned_views_use_native_identity(
    monkeypatch,
    tmp_path: Path,
    workspace_factory,
    suffix: str,
    kind: str,
    language: str,
    view: str,
) -> None:
    executable = tmp_path / "cdsTextTo5x"
    executable.write_text("tool\n", encoding="utf-8")
    executable.chmod(0o755)
    resources = Resources(
        tools={"cadence.cds-text-to-5x": str(executable)}
    )
    source = tmp_path / f"model.{suffix}"
    source.write_text("module model; endmodule\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    work_dir = tmp_path / "tool-work"
    commands: list[list[str]] = []

    class Library:
        @staticmethod
        def get(_name, **_kwargs):
            return type("LibraryInfo", (), {"path": tmp_path / "virtuoso" / "lib"})()

    client = type("Client", (), {"library": Library()})()
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.virtuoso_workdir",
        lambda _client: operation.root,
    )

    def run(request):
        commands.append(request.argv)
        request.before_spawn()
        return ProcessResult(returncode=0, stdout="ok", stderr="")

    with workspace_factory(client, library="lib") as operation:
        (operation.root / "cds.lib").write_text("# test\n", encoding="utf-8")
        (operation.root / "lib").mkdir()
        with operation.mutation_scope(
            "lib", cells=("model",), phase="source-owned text-view proof"
        ):
            import_oa_text_view(
                client,
                library="lib",
                cell="model",
                kind=kind,
                view=view,
                source=source,
                resources=resources,
                log_dir=log_dir,
                work_dir=work_dir,
                operation=operation,
                process=SimpleNamespace(run=run),
            )

    command = commands[0]
    assert command[command.index("-LANG") + 1] == language
    assert command[command.index("-VIEW") + 1] == view
