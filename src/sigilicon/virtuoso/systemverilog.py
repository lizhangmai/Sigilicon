"""Single-cell SystemVerilog text-view import adapter."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Any

from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    owned_output_file,
    run_process_group,
)
from sigilicon.virtuoso.capability import require_oa_target_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import skill_quote, virtuoso_workdir


def import_systemverilog_view(
    client: Any,
    *,
    library: str,
    cell: str,
    source: Path,
    source_sha256: str,
    log_dir: Path,
    work_dir: Path,
    operation: Any,
    overwrite: bool = False,
    timeout: int = 300,
) -> None:
    """Import one source file through a process-group-owned Cadence adapter."""

    require_oa_target_capability(
        operation,
        client,
        library=library,
        cell=cell,
        phase="SystemVerilog text-view adapter",
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
    if overwrite:
        operation.require_active_mutation(
            client,
            library,
            cell,
            phase="remove generated SystemVerilog view",
        )
        source_code = f'''let((obj deleted)
  obj = ddGetObj({skill_quote(library)} {skill_quote(cell)} "systemVerilog")
  when(obj
    deleted = ddDeleteObj(obj)
    unless(deleted error("generated SystemVerilog view delete failed"))
    obj = nil)
  t
)'''
        deletion = require_bridge_confirmation(
            operation,
            f"remove generated SystemVerilog view {library}/{cell}",
            lambda: client.execute_skill(source_code, timeout=60),
        )
        if deletion.errors:
            raise RuntimeError(deletion.errors[0])
    with (
        owned_input_file(source, expected_sha256=source_sha256) as owned_source,
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
        command = [
            str(executable),
            "-CDSLIB",
            owned_cds_lib.child_path,
            "-LANG",
            "systemverilog",
            "-LIB",
            library,
            "-CELL",
            cell,
            "-VIEW",
            "systemVerilog",
            "-LOG",
            owned_tool_log.child_path,
            owned_source.child_path,
        ]
        completed = run_process_group(
            command,
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
        with owned_output_file(
            owned_logs,
            "cdsTextTo5x.stdout.log",
        ) as owned_stdout:
            owned_stdout.write_bytes(
                ((completed.stdout or "") + (completed.stderr or "")).encode(
                    "utf-8", errors="replace"
                )
            )
        tool_log = owned_tool_log.read_bytes().decode("utf-8", errors="replace")
        diagnostics = (
            tool_log + "\n" + (completed.stdout or "") + (completed.stderr or "")
        )
        log_error = re.search(
            r"(?m)^(?:ERROR\s|\*Error\*|[^\n:]+:\s+\*E,)",
            diagnostics,
        )
        if completed.returncode != 0 or log_error is not None:
            tail = diagnostics[-6000:]
            raise RuntimeError(
                f"cdsTextTo5x failed for {library}/{cell}/systemVerilog\n{tail}"
            )
