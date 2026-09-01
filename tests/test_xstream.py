from __future__ import annotations

from pathlib import Path
import struct
import subprocess

import pytest

from sigilicon.virtuoso.xstream import (
    XStreamExportError,
    XStreamExportRequest,
    canonicalize_xstream_gdsii,
    run_xstream_export,
)


def _request(tmp_path: Path) -> XStreamExportRequest:
    executable = tmp_path / "cadence" / "tools" / "bin" / "strmout"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)
    layer_map = tmp_path / "layer.map"
    layer_map.write_text("M1 drawing 1 0\n", encoding="utf-8")
    cds_lib = tmp_path / "cds.lib"
    cds_lib.write_text("DEFINE scratch ./scratch\n", encoding="utf-8")
    return XStreamExportRequest(
        executable=executable,
        library="scratch",
        cell="neutral",
        view="layout",
        technology_library="techLib",
        layer_map=layer_map,
        cds_lib=cds_lib,
        work_root=tmp_path / "run",
        flatten_pcells=True,
        suppressed_warnings=("XSTRM-35",),
    )


def _write_success(cwd: Path) -> None:
    (cwd / "strmout.log").write_text(
        "Translation completed. '0' error(s) and '0' warning(s) found.\n",
        encoding="utf-8",
    )
    (cwd / "strmout.sum").write_text("complete\n", encoding="utf-8")
    (cwd / "layout.gds").write_bytes(b"non-empty-gds")


def _record(record_type: int, data_type: int = 0, data: bytes = b"") -> bytes:
    return struct.pack(">HBB", len(data) + 4, record_type, data_type) + data


def _gds_string(value: str) -> bytes:
    encoded = value.encode("ascii")
    return encoded + (b"\0" if len(encoded) % 2 else b"")


def _xstream_pcell_gds(volatile_identity: str) -> bytes:
    generated = f"unit_CDNS_{volatile_identity}"
    date = struct.pack(">12H", *(2026, 8, 27, 1, 2, 3) * 2)
    return b"".join(
        (
            _record(0x00, 0x02, struct.pack(">H", 600)),
            _record(0x01, 0x02, date),
            _record(0x02, 0x06, _gds_string("LIB")),
            _record(0x03, 0x05, bytes(16)),
            _record(0x05, 0x02, date),
            _record(0x06, 0x06, _gds_string(generated)),
            _record(0x08),
            _record(0x0D, 0x02, struct.pack(">H", 1)),
            _record(0x0E, 0x02, struct.pack(">H", 0)),
            _record(0x10, 0x03, struct.pack(">10i", 0, 0, 10, 0, 10, 10, 0, 10, 0, 0)),
            _record(0x11),
            _record(0x07),
            _record(0x05, 0x02, date),
            _record(0x06, 0x06, _gds_string("TOP")),
            _record(0x0A),
            _record(0x12, 0x06, _gds_string(generated)),
            _record(0x10, 0x03, struct.pack(">2i", 0, 0)),
            _record(0x11),
            _record(0x07),
            _record(0x04),
        )
    )


def test_xstream_gdsii_canonicalizes_volatile_pcell_hierarchy_names() -> None:
    first = canonicalize_xstream_gdsii(
        _xstream_pcell_gds("787838128820")
    )
    second = canonicalize_xstream_gdsii(
        _xstream_pcell_gds("787838266050")
    )

    assert first == second
    assert b"TOP" in first
    assert b"787838128820" not in first
    assert b"787838266050" not in second


def test_xstream_export_uses_owned_inputs_and_authoritative_completion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    observed: dict[str, object] = {}

    def runner(command, **kwargs):
        kwargs["before_spawn"]()
        observed.update(command=command, **kwargs)
        _write_success(kwargs["cwd"])
        return subprocess.CompletedProcess(command, 0, "translator stdout")

    monkeypatch.setattr(
        "sigilicon.virtuoso.xstream.run_process_group", runner
    )

    result = run_xstream_export(
        request,
        environment={"PATH": "/snapshot/bin", "CDS_LIC_FILE": "snapshot"},
    )

    assert result.exit_code == 0
    assert result.gds_path.read_bytes() == b"non-empty-gds"
    assert observed["cwd"] == request.work_root
    assert observed["env"]["CDS_LIC_FILE"] == "snapshot"
    assert observed["env"]["PATH"] == "/snapshot/bin"
    command = tuple(observed["command"])
    assert command[command.index("-library") + 1] == "scratch"
    assert command[command.index("-topCell") + 1] == "neutral"
    assert command[command.index("-view") + 1] == "layout"
    assert command[command.index("-techLib") + 1] == "techLib"
    assert "-flattenPcells" in command
    assert command[command.index("-noWarn") + 1] == "35"
    assert len(observed["pass_fds"]) == 5


def test_xstream_export_preserves_explicit_multicall_launcher_symlink(
    monkeypatch,
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    wrapper = tmp_path / "cadence" / "share" / "bin" / "cdnWrapperWithOA"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    wrapper.chmod(0o755)
    request.executable.unlink()
    request.executable.symlink_to(wrapper)
    request = XStreamExportRequest(
        executable=request.executable,
        library=request.library,
        cell=request.cell,
        view=request.view,
        technology_library=request.technology_library,
        layer_map=request.layer_map,
        cds_lib=request.cds_lib,
        work_root=request.work_root,
    )
    observed: dict[str, object] = {}

    def runner(command, **kwargs):
        kwargs["before_spawn"]()
        observed["command"] = command
        _write_success(kwargs["cwd"])
        return subprocess.CompletedProcess(command, 0, "translator stdout")

    monkeypatch.setattr(
        "sigilicon.virtuoso.xstream.run_process_group", runner
    )

    run_xstream_export(request)

    assert request.executable.is_symlink()
    assert tuple(observed["command"])[0] == str(request.executable)


@pytest.mark.parametrize(
    "failure,expected",
    (
        ("nonzero", "exited 9"),
        ("missing-summary", "regular summary"),
        ("unproven", "does not prove"),
        ("symlink-gds", "regular GDSII output"),
    ),
)
def test_xstream_export_separates_failed_or_unproven_outputs(
    monkeypatch,
    tmp_path: Path,
    failure: str,
    expected: str,
) -> None:
    request = _request(tmp_path)

    def runner(command, **kwargs):
        kwargs["before_spawn"]()
        cwd = kwargs["cwd"]
        if failure == "symlink-gds":
            target = tmp_path / "outside.gds"
            target.write_bytes(b"outside")
            (cwd / "strmout.log").write_text(
                "Translation completed. '0' error(s) and '0' warning(s) found.\n",
                encoding="utf-8",
            )
            (cwd / "strmout.sum").write_text("complete\n", encoding="utf-8")
            (cwd / "layout.gds").symlink_to(target)
        else:
            _write_success(cwd)
        if failure == "missing-summary":
            (cwd / "strmout.sum").unlink()
        elif failure == "unproven":
            (cwd / "strmout.log").write_text("no completion proof\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            9 if failure == "nonzero" else 0,
            "translator rejected one option\n" if failure == "nonzero" else "",
        )

    monkeypatch.setattr(
        "sigilicon.virtuoso.xstream.run_process_group", runner
    )

    with pytest.raises(XStreamExportError, match=expected) as error:
        run_xstream_export(request)

    assert error.value.executed
    assert error.value.exit_code == (9 if failure == "nonzero" else 0)
    if failure == "nonzero":
        assert error.value.diagnostic_path == request.work_root / "xstream-failure.log"
        diagnostic = error.value.diagnostic_path.read_text(encoding="utf-8")
        assert "exit_code=9" in diagnostic
        assert "translator rejected one option" in diagnostic
