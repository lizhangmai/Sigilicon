"""Create-only Cadence OA text-view import adapter."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Any

from sigilicon.domain.source import TextSourceSnapshot, load_text_source_snapshot
from sigilicon.virtuoso.bridge import decode_skill_output

from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_output_file,
    owned_sealed_input,
    run_process_group,
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
    timeout: int = 300,
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
    cds_home = os.environ.get("CDSHOME")
    candidates = [Path(found) for found in [shutil.which("cdsTextTo5x")] if found]
    if cds_home:
        candidates.append(Path(cds_home) / "tools" / "dfII" / "bin" / "cdsTextTo5x")
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise FileNotFoundError("cdsTextTo5x was not found in PATH or CDSHOME")
    library_path = operation.require_project_library_target(client, library)
    with (
        owned_sealed_input(
            snapshot.text.encode("utf-8"),
            name=snapshot.source_path.name,
        ) as owned_source,
        owned_directory(workdir) as owned_workdir,
        owned_directory(library_path) as owned_library,
        owned_directory(work_dir, create_missing=True) as owned_tool_work,
        owned_output_file(owned_tool_work, "cds.lib") as owned_cds_lib,
        owned_directory(log_dir) as owned_logs,
        owned_output_file(owned_logs, "cdsTextTo5x.log") as owned_tool_log,
    ):
        owned_cds_lib.write_bytes(
            f"DEFINE {library} {owned_library.child_path}\n".encode("utf-8")
        )
        os.fchmod(owned_cds_lib.fd, 0o444)
        completed = run_process_group(
            [
                str(executable),
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
                owned_source.child_path,
            ],
            cwd=Path(owned_workdir.child_path),
            env=cadence_subprocess_env(),
            timeout=timeout,
            before_spawn=lambda: operation.require_active_mutation(
                client,
                library,
                cell,
                phase="cdsTextTo5x process spawn",
            ),
            pass_fds=(
                owned_source.fd,
                owned_cds_lib.fd,
                owned_workdir.fd,
                owned_library.fd,
                owned_tool_work.fd,
                owned_logs.fd,
                owned_tool_log.fd,
            ),
        )
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
