"""Create-only Cadence OA text-view import adapter."""

from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import re
from typing import Any

from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot

from sigilicon.external_tools import (
    CADENCE_TEXT_IMPORT_TOOL,
    ProcessPort,
    ProcessRequest,
    cadence_ic_env,
    managed_process,
    owned_directory,
    owned_input_file,
    owned_output_file,
)
from sigilicon.virtuoso.capability import (
    dispatch_oa_mutation,
    require_oa_target_capability,
)
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    skill_quote,
    virtuoso_workdir,
)


_TEXT_VIEW_ADAPTERS = {
    "spectre_model": ("spectre", "spectre"),
    "system_verilog": ("systemverilog", "systemVerilog"),
    # cdsTextTo5x exposes Verilog-A through its Verilog-AMS front end.  In
    # particular, IC25.1 rejects ``-LANG veriloga`` while retaining the native
    # OA view name ``veriloga``.
    "veriloga": ("verilogams", "veriloga"),
}

# DDPI can create a view master only for a filename registered in Cadence's
# Data Registry.  ``text.txt`` is the native master for the generic ``text``
# view type used by source-owned SKILL measurement views.
_SKILL_OA_MASTER = "text.txt"


def _native_master_name(directory: Path) -> str:
    with owned_input_file(directory / "master.tag") as tag:
        lines = os.pread(tag.fd, os.fstat(tag.fd).st_size, 0).decode("utf-8").splitlines()
    master = lines[-1].strip() if lines else ""
    if not master or Path(master).name != master or master in {".", ".."}:
        raise RuntimeError("invalid native text-view master name")
    return master


def check_oa_text_view_source(directory: Path, source: TextSourceSnapshot) -> None:
    """Verify that a native master persists the exact canonical text locally."""

    with owned_directory(directory) as view:
        master = _native_master_name(view.path)
        with owned_input_file(view.path / master) as native:
            actual = os.pread(native.fd, os.fstat(native.fd).st_size, 0)
            if actual != source.text.encode("utf-8"):
                raise RuntimeError("native text-view master differs from its source")


def _import_skill_view(
    client: Any,
    *,
    library: str,
    cell: str,
    view: str,
    source: TextSourceSnapshot,
    operation: Any,
    timeout: int,
) -> None:
    """Create a generic OA text view from a canonical SKILL source."""

    if view != "measurement":
        raise ValueError("SKILL measurement views must use the OA view name measurement")
    payload = source.text
    skill = f'''let((fileObj viewObj port path complete attempt)
  fileObj = nil
  viewObj = nil
  port = nil
  complete = nil
  when(ddGetObj({skill_quote(library)} {skill_quote(cell)} {skill_quote(view)})
    error("refusing to replace existing SKILL view"))
  attempt = errset(
    unwindProtect(
      progn(
        fileObj = ddGetObj({skill_quote(library)} {skill_quote(cell)}
          {skill_quote(view)} {skill_quote(_SKILL_OA_MASTER)} nil "w")
        unless(fileObj error("cannot create SKILL master file"))
        path = ddGetObjWritePath(fileObj)
        unless(path error("cannot resolve SKILL master write path"))
        port = outfile(path)
        unless(port error("cannot open SKILL master for writing"))
        fprintf(port "%s" {skill_quote(payload)})
        unless(close(port) error("cannot close SKILL master"))
        port = nil
        ; DDPI mode "w" marks the first registered file in a view as its
        ; master.  Re-marking the generic text master conflicts with the
        ; AsciiText view type in IC25.1.
        ddReleaseObj(fileObj)
        fileObj = nil
        unless(ddGetObj({skill_quote(library)} {skill_quote(cell)}
          {skill_quote(view)} "*")
          error("SKILL OA master is not discoverable"))
        complete = t)
      progn(
        when(port errset(close(port) nil) port = nil)
        when(fileObj ddReleaseObj(fileObj) fileObj = nil)
        unless(complete
          viewObj = ddGetObj({skill_quote(library)} {skill_quote(cell)}
            {skill_quote(view)})
          when(viewObj errset(ddDeleteObj(viewObj) nil)))))
    nil)
  unless(attempt && car(attempt) && complete
    error("SKILL OA view creation failed"))
  t)'''
    result = require_bridge_confirmation(
        operation,
        f"create SKILL view {library}/{cell}/{view}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=cell,
            phase=f"SKILL view creation {library}/{cell}/{view}",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    skill,
                    label=f"SKILL view creation {library}/{cell}/{view}",
                    mutation_target=(library, (cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create {library}/{cell}/{view}: {result.errors[0]}"
        )


def import_oa_text_view(
    client: Any,
    *,
    library: str,
    cell: str,
    kind: str,
    view: str,
    source: Path | TextSourceSnapshot,
    log_dir: Path,
    work_dir: Path,
    operation: Any,
    resources: Any,
    timeout: int = 300,
    process: ProcessPort = managed_process,
) -> None:
    """Import one canonical model source without replacing an existing view."""

    snapshot = (
        source
        if isinstance(source, TextSourceSnapshot)
        else load_text_source_snapshot(source)
    )
    if kind == "skill":
        require_oa_target_capability(
            operation,
            client,
            library=library,
            cell=cell,
            phase=f"{kind} measurement text-view adapter",
        )
        _import_skill_view(
            client,
            library=library,
            cell=cell,
            view=view,
            source=snapshot,
            operation=operation,
            timeout=timeout,
        )
        return

    try:
        language, native_view = _TEXT_VIEW_ADAPTERS[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported OA text-view kind: {kind}") from exc
    if view != native_view:
        raise ValueError(
            f"{kind} must use the native OA view name {native_view}, not {view}"
        )
    require_oa_target_capability(
        operation,
        client,
        library=library,
        cell=cell,
        phase=f"{kind} text-view adapter",
    )
    workdir = virtuoso_workdir(client)
    executable = resources.configured_tool(CADENCE_TEXT_IMPORT_TOOL)
    if executable is None:
        raise FileNotFoundError(
            f"runtime.tools.{CADENCE_TEXT_IMPORT_TOOL} must name an available executable"
        )
    library_path = operation.require_project_library_target(client, library)
    with (
        resources.owned_tool(CADENCE_TEXT_IMPORT_TOOL) as owned_launcher,
        owned_directory(workdir) as owned_workdir,
        owned_directory(library_path) as owned_library,
        owned_directory(work_dir, create_missing=True) as owned_tool_work,
        owned_output_file(owned_tool_work, "source" + snapshot.source_path.suffix) as staged_source,
        owned_output_file(owned_tool_work, "cds.lib") as owned_cds_lib,
        owned_directory(log_dir) as owned_logs,
        owned_output_file(owned_logs, "cdsTextTo5x.log") as owned_tool_log,
        ExitStack() as source_lifetime,
    ):
        # Cadence canonicalizes the source path and reopens it in its parser.
        # Keep the planned bytes in a private named file, watched throughout
        # the invocation; an anonymous memfd cannot satisfy that protocol.
        staged_source.write_bytes(snapshot.text.encode("utf-8"))
        owned_source = source_lifetime.enter_context(owned_input_file(staged_source.path))
        owned_cds_lib.write_bytes(
            f"DEFINE {library} {owned_library.child_path}\n".encode("utf-8")
        )
        command = (
            *owned_launcher.command,
            "-CDSLIB",
            owned_cds_lib.child_path,
            "-LANG",
            language,
            "-LIB",
            library,
            "-CELL",
            cell,
            "-VIEW",
            native_view,
            "-LOG",
            owned_tool_log.child_path,
            owned_source.child_named_path,
        )

        def validate_spawn() -> None:
            owned_launcher.require_visible()
            owned_source.require_visible()
            operation.require_active_mutation(
                client,
                library,
                cell,
                phase="cdsTextTo5x process spawn",
            )

        completed = process.run(ProcessRequest(
            argv=tuple(command),
            executable=owned_launcher.executable,
            cwd=Path(owned_workdir.child_path),
            environment=cadence_ic_env(
                executable,
                resources.environment,
                xrun=resources.configured_tool("cadence.xrun"),
            ),
            timeout_seconds=timeout,
            before_spawn=validate_spawn,
            pass_fds=(
                owned_source.fd,
                owned_source.directory_fd,
                owned_cds_lib.fd,
                owned_workdir.fd,
                owned_library.fd,
                owned_tool_work.fd,
                owned_logs.fd,
                owned_tool_log.fd,
            ),
        ))
        with owned_output_file(owned_logs, "cdsTextTo5x.stdout.log") as owned_stdout:
            owned_stdout.write_bytes(
                ((completed.stdout or "") + (completed.stderr or "")).encode(
                    "utf-8", errors="replace"
                )
            )
        diagnostics = (
            owned_tool_log.read_bytes().decode("utf-8", errors="replace")
            + "\n"
            + (completed.stdout or "")
            + (completed.stderr or "")
        )
        log_error = re.search(
            r"(?m)^(?:ERROR\s|\*Error\*|[^\n:]+:\s+\*E,)", diagnostics
        )
        if completed.returncode != 0 or log_error is not None:
            raise RuntimeError(
                f"cdsTextTo5x failed for {library}/{cell}/{native_view}\n"
                f"{diagnostics[-6000:]}"
            )
        # cdsTextTo5x links the registered master to its input.  The source
        # scope is temporary, so materialize the planned bytes in the native
        # view before releasing that scope.
        with owned_directory(library_path / cell / native_view) as native_directory:
            master = _native_master_name(native_directory.path)
            target = os.readlink(master, dir_fd=native_directory.fd)
            if target != str(staged_source.path):
                raise RuntimeError("cdsTextTo5x native master does not link its planned source")
            operation.require_active_mutation(
                client, library, cell, phase="persist native text-view master",
            )
            os.unlink(master, dir_fd=native_directory.fd)
            with owned_output_file(native_directory, master) as native_source:
                native_source.write_bytes(snapshot.text.encode("utf-8"))
