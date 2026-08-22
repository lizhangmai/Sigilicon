from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from sigilicon.virtuoso.systemverilog import import_systemverilog_view
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


def test_systemverilog_external_write_revalidates_at_spawn(
    monkeypatch,
    tmp_path: Path,
    workspace_factory,
) -> None:
    executable = tmp_path / "cdsTextTo5x"
    executable.write_text("tool\n", encoding="utf-8")
    source = tmp_path / "tb.sv"
    source.write_text("module tb; endmodule\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    work_dir = tmp_path / "tool-work"
    events: list[str] = []

    class Library:
        @staticmethod
        def get(_name, **_kwargs):
            return type("LibraryInfo", (), {"path": tmp_path / "virtuoso" / "lib"})()

    client = type("Client", (), {"library": Library()})()

    monkeypatch.setattr(
        "sigilicon.virtuoso.systemverilog.shutil.which",
        lambda _name: str(executable),
    )

    def run(command, **kwargs):
        proc_fd_prefix = f"/proc/{os.getpid()}/fd/"
        events.append("runner-enter")
        assert all(os.fstat(fd) for fd in kwargs["pass_fds"])
        assert command[-1].startswith(proc_fd_prefix)
        assert Path(command[-1]).samefile(source)
        assert command[2].startswith(proc_fd_prefix)
        assert Path(command[2]).samefile(work_dir / "cds.lib")
        library_target = Path(command[2]).read_text(encoding="utf-8").split()[2]
        assert library_target.startswith(proc_fd_prefix)
        assert Path(library_target).samefile(operation.root / "lib")
        assert kwargs["cwd"].as_posix().startswith(proc_fd_prefix)
        kwargs["before_spawn"]()
        events.append("spawn-authorized")
        return subprocess.CompletedProcess(command, 0, "ok", None)

    monkeypatch.setattr("sigilicon.virtuoso.systemverilog.run_process_group", run)

    with workspace_factory(client, library="lib") as operation:
        (operation.root / "cds.lib").write_text("# test\n", encoding="utf-8")
        (operation.root / "lib").mkdir()
        monkeypatch.setattr(
            "sigilicon.virtuoso.systemverilog.virtuoso_workdir",
            lambda _client: operation.root,
        )
        original_dispatch = operation.require_active_mutation

        def record_dispatch(*args, **kwargs):
            events.append("target-revalidated")
            return original_dispatch(*args, **kwargs)

        operation.require_active_mutation = record_dispatch
        with operation.mutation_scope(
            "lib",
            cells=("tb",),
            phase="SystemVerilog adapter proof",
        ):
            import_systemverilog_view(
                client,
                library="lib",
                cell="tb",
                source=source,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                log_dir=log_dir,
                work_dir=work_dir,
                operation=operation,
            )

    assert events == ["runner-enter", "target-revalidated", "spawn-authorized"]
    assert (log_dir / "cdsTextTo5x.stdout.log").read_text(encoding="utf-8") == "ok"
    assert (work_dir / "cds.lib").read_text(encoding="utf-8").startswith("DEFINE lib ")


def test_source_owned_text_view_uses_native_systemverilog_identity(
    monkeypatch,
    tmp_path: Path,
    workspace_factory,
) -> None:
    executable = tmp_path / "cdsTextTo5x"
    executable.write_text("tool\n", encoding="utf-8")
    source = tmp_path / "model.sv"
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
        "sigilicon.virtuoso.text_view.shutil.which", lambda _name: str(executable)
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.virtuoso_workdir",
        lambda _client: operation.root,
    )

    def run(command, **kwargs):
        commands.append(command)
        kwargs["before_spawn"]()
        return subprocess.CompletedProcess(command, 0, "ok", None)

    monkeypatch.setattr("sigilicon.virtuoso.text_view.run_process_group", run)

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
                kind="system_verilog",
                view="systemVerilog",
                source=source,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                log_dir=log_dir,
                work_dir=work_dir,
                operation=operation,
            )

    command = commands[0]
    assert command[command.index("-LANG") + 1] == "systemverilog"
    assert command[command.index("-VIEW") + 1] == "systemVerilog"


def test_source_owned_spectre_model_uses_native_stop_view_identity(
    monkeypatch,
    tmp_path: Path,
    workspace_factory,
) -> None:
    executable = tmp_path / "cdsTextTo5x"
    executable.write_text("tool\n", encoding="utf-8")
    source = tmp_path / "model.scs"
    source.write_text("subckt model A B\nends model\n", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    commands: list[list[str]] = []

    class Library:
        @staticmethod
        def get(_name, **_kwargs):
            return type("LibraryInfo", (), {"path": operation.root / "lib"})()

    client = type("Client", (), {"library": Library()})()
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.shutil.which", lambda _name: str(executable)
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.virtuoso_workdir",
        lambda _client: operation.root,
    )

    def run(command, **kwargs):
        commands.append(command)
        kwargs["before_spawn"]()
        return subprocess.CompletedProcess(command, 0, "ok", None)

    monkeypatch.setattr("sigilicon.virtuoso.text_view.run_process_group", run)

    with workspace_factory(client, library="lib") as operation:
        (operation.root / "cds.lib").write_text("# test\n", encoding="utf-8")
        (operation.root / "lib").mkdir()
        with operation.mutation_scope(
            "lib", cells=("model",), phase="source-owned Spectre model proof"
        ):
            import_oa_text_view(
                client,
                library="lib",
                cell="model",
                kind="spectre_model",
                view="spectre",
                source=source,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                log_dir=tmp_path / "logs",
                work_dir=tmp_path / "work",
                operation=operation,
            )

    command = commands[0]
    assert command[command.index("-LANG") + 1] == "spectre"
    assert command[command.index("-VIEW") + 1] == "spectre"


def test_source_owned_veriloga_uses_verilogams_import_language(
    monkeypatch,
    tmp_path: Path,
    workspace_factory,
) -> None:
    executable = tmp_path / "cdsTextTo5x"
    executable.write_text("tool\n", encoding="utf-8")
    source = tmp_path / "model.va"
    source.write_text("module model(p, n); inout p, n; endmodule\n", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    commands: list[list[str]] = []

    class Library:
        @staticmethod
        def get(_name, **_kwargs):
            return type("LibraryInfo", (), {"path": operation.root / "lib"})()

    client = type("Client", (), {"library": Library()})()
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.shutil.which", lambda _name: str(executable)
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.text_view.virtuoso_workdir",
        lambda _client: operation.root,
    )

    def run(command, **kwargs):
        commands.append(command)
        kwargs["before_spawn"]()
        return subprocess.CompletedProcess(command, 0, "ok", None)

    monkeypatch.setattr("sigilicon.virtuoso.text_view.run_process_group", run)

    with workspace_factory(client, library="lib") as operation:
        (operation.root / "cds.lib").write_text("# test\n", encoding="utf-8")
        (operation.root / "lib").mkdir()
        with operation.mutation_scope(
            "lib", cells=("model",), phase="source-owned Verilog-A proof"
        ):
            import_oa_text_view(
                client,
                library="lib",
                cell="model",
                kind="veriloga",
                view="veriloga",
                source=source,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                log_dir=tmp_path / "logs",
                work_dir=tmp_path / "work",
                operation=operation,
            )

    command = commands[0]
    assert command[command.index("-LANG") + 1] == "verilogams"
    assert command[command.index("-VIEW") + 1] == "veriloga"
