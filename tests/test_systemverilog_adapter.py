from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest

from sigilicon.execution._resources import Resources
from sigilicon.domain.source import load_text_source_snapshot
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
    executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1]
# The parser canonicalizes the supplied path, then reopens it in a child
# without inherited descriptors, as the real Cadence importer does.
source = os.path.realpath(args[-1])
payload = subprocess.check_output([
    sys.executable, "-c", "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(), end='')", source,
], close_fds=True, text=True)
if option("-LANG") != "spectre":
    for tool in ("ncroot", "xrun"):
        assert subprocess.check_output([tool], text=True).strip() == tool
library_path = Path(Path(option("-CDSLIB")).read_text().split()[2]).resolve()
native_directory = library_path / option("-CELL") / option("-VIEW")
native_directory.mkdir(parents=True)
master = {"spectre": "spectre.scs", "systemverilog": "verilog.sv", "verilogams": "veriloga.va"}[option("-LANG")]
(native_directory / "master.tag").write_text("-- Master.tag File, Rev:1.0\n" + master + "\n")
(native_directory / master).symlink_to(source)
Path(option("-LOG")).write_text(json.dumps({
    "language": option("-LANG"), "view": option("-VIEW"), "source": payload,
}))
''', encoding="utf-8")
    executable.chmod(0o755)
    tools = {"cadence.cds-text-to-5x": str(executable)}
    if kind != "spectre_model":
        xcelium_bin = tmp_path / "Xcelium" / "tools" / "bin"
        xcelium_bin.mkdir(parents=True)
        for name in ("ncroot", "xrun"):
            helper = xcelium_bin / name
            helper.write_text(f"#!{sys.executable}\nprint({name!r})\n")
            helper.chmod(0o755)
        tools["cadence.xrun"] = str(xcelium_bin / "xrun")
    resources = Resources(
        tools=tools
    )
    source = tmp_path / f"model.{suffix}"
    expected_source = "module model; endmodule\n"
    source.write_text(expected_source, encoding="utf-8")
    snapshot = load_text_source_snapshot(source)
    source.write_text("source changed after planning\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    work_dir = tmp_path / "tool-work"

    class Library:
        @staticmethod
        def get(_name, **_kwargs):
            return type("LibraryInfo", (), {"path": tmp_path / "virtuoso" / "lib"})()

    client = type("Client", (), {"library": Library()})()
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.virtuoso_workdir",
        lambda _client: operation.root,
    )

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
                source=snapshot,
                resources=resources,
                log_dir=log_dir,
                work_dir=work_dir,
                operation=operation,
            )

    imported = json.loads((log_dir / "cdsTextTo5x.log").read_text())
    assert imported["language"] == language
    assert imported["view"] == view
    assert imported["source"] == expected_source
    master = {"spectre": "spectre.scs", "systemverilog": "verilog.sv", "verilogams": "veriloga.va"}[language]
    native_source = operation.root / "lib" / "model" / view / master
    (work_dir / f"source.{suffix}").unlink()
    assert not native_source.is_symlink()
    assert native_source.read_text() == expected_source
