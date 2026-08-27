from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from sigilicon.virtuoso.xstream import (
    XStreamExportError,
    XStreamExportRequest,
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

    monkeypatch.setattr("sigilicon.virtuoso.xstream.run_process_group", runner)

    result = run_xstream_export(request)

    assert result.exit_code == 0
    assert result.gds_path.read_bytes() == b"non-empty-gds"
    assert observed["cwd"] == request.work_root
    command = tuple(observed["command"])
    assert command[command.index("-library") + 1] == "scratch"
    assert command[command.index("-topCell") + 1] == "neutral"
    assert command[command.index("-view") + 1] == "layout"
    assert command[command.index("-techLib") + 1] == "techLib"
    assert "-flattenPcells" in command
    assert command[command.index("-noWarn") + 1] == "35"
    assert len(observed["pass_fds"]) == 5


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
            "",
        )

    monkeypatch.setattr("sigilicon.virtuoso.xstream.run_process_group", runner)

    with pytest.raises(XStreamExportError, match=expected) as error:
        run_xstream_export(request)

    assert error.value.executed
    assert error.value.exit_code == (9 if failure == "nonzero" else 0)
