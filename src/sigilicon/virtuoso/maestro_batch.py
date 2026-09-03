"""Isolated, headless Maestro execution outside the user's Virtuoso process."""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping

from sigilicon.external_tools import (
    CADENCE_VIRTUOSO_TOOL,
    ProcessGroupCleanupUncertainError,
    cadence_ic_env,
    owned_atomic_output_file,
    owned_directory,
    owned_input_file,
    owned_output_file,
    owned_sealed_input,
    run_process_group_until_confirmed,
)
from sigilicon.paths import validate_artifact_component, validate_artifact_id
from sigilicon.virtuoso.capability import (
    WorkspaceAuthority,
    require_workspace_capability,
)
from sigilicon.virtuoso.oa import skill_quote


@dataclass(frozen=True)
class IsolatedMaestroRunResult:
    history: str
    status: str
    stdout: str
    worker_log: Path
    worker_log_text: str
    control_script: str
    rdb_export: Path
    terminated_after_completion: bool = False


_CDS_INCLUDE = re.compile(r"^(\s*(?:SOFTINCLUDE|INCLUDE)\s+)(\S+)(\s*)$")
_CDS_DEFINE = re.compile(r"^(\s*DEFINE\s+\S+\s+)(\S+)(\s*)$")


def _canonical_worker_cds_lib(
    source: str,
    *,
    workspace: Path,
    resources: ExitStack,
) -> tuple[str, tuple[int, ...]]:
    """Bind top-level cds.lib paths to held owner-process descriptors."""

    descriptors: list[int] = []
    rendered: list[str] = []
    for line in source.splitlines():
        match = _CDS_INCLUDE.match(line)
        kind = "include"
        if match is None:
            match = _CDS_DEFINE.match(line)
            kind = "define"
        if match is None:
            rendered.append(line)
            continue
        token = match.group(2)
        if token.startswith("$"):
            rendered.append(line)
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = Path(os.path.abspath(workspace / candidate))
        if kind == "include":
            owned = resources.enter_context(
                owned_input_file(candidate, require_single_link=False)
            )
            replacement = owned.child_named_path
            descriptors.extend((owned.fd, owned.directory_fd))
        elif candidate.is_dir():
            owned_directory_value = resources.enter_context(
                owned_directory(candidate)
            )
            replacement = owned_directory_value.child_path
            descriptors.append(owned_directory_value.fd)
        elif Path(token).is_absolute():
            # Cadence permits definitions for libraries that are not currently
            # installed.  Preserve an already-absolute missing target rather
            # than silently rebasing it under the procfd namespace.
            replacement = token
        else:
            raise RuntimeError(f"relative cds.lib directory does not exist: {token}")
        rendered.append(f"{match.group(1)}{replacement}{match.group(3)}")
    return "\n".join(rendered) + "\n", tuple(descriptors)


def render_isolated_maestro_run_skill(
    library: str,
    cell: str,
    *,
    variables: Mapping[str, str],
    simulation_root: Path | str,
    nonce: str,
    rdb_export: Path | str,
) -> str:
    """Render one worker using native Maestro completion and RDB export."""

    validate_artifact_component(library, "Maestro worker library")
    validate_artifact_component(cell, "Maestro worker testbench")
    validate_artifact_id(nonce, "Maestro worker nonce")
    root = Path(os.path.abspath(simulation_root))
    if root == Path(root.anchor):
        raise ValueError("Maestro worker simulation root must not be filesystem root")
    assignments = "\n".join(
        f"        maeSetVar({skill_quote(name)} {skill_quote(value)} "
        "?session session)"
        for name, value in sorted(variables.items())
    )
    if assignments:
        assignments += "\n"

    rdb = f'''        resultPort = outfile({skill_quote(str(rdb_export))})
        unless(resultPort error("cannot open native Maestro RDB export"))
        resultsOpened = maeOpenResults(?history history ?session session)
        unless(resultsOpened error("cannot open native Maestro results"))
        resultDb = maeReadResDB(?historyName history ?session session)
        unless(resultDb error("cannot open native Maestro result database"))
        resultPoints = resultDb->points()
        resultExpressionCount = 0
        fprintf(resultPort "RDB_SCHEMA\\t1\\n")
        foreach(resultPoint resultPoints
          foreach(resultParam resultPoint->params()
            fprintf(resultPort "PARAM\\t%d\\t%s\\t%L\\n"
              resultPoint->id resultParam->name resultParam->value)
          )
          foreach(resultOutput resultPoint->outputs(?type 'expr ?sortBy 'corner)
            resultExpressionCount = resultExpressionCount + 1
            resultSpecStatus = maeGetSpecStatus(
              resultOutput->name resultOutput->testName
              ?pointId resultOutput->pointID)
            fprintf(resultPort "OUTPUT\\t%d\\t%s\\t%s\\t%s\\t%L\\t%L\\n"
              resultOutput->pointID resultOutput->cornerName
              resultOutput->testName resultOutput->name
              resultOutput->value resultSpecStatus)
          )
        )
        fprintf(resultPort "SUMMARY\\t%d\\t%d\\n"
          length(resultPoints) resultExpressionCount)
        fprintf(resultPort "OVERALL_SPEC\\t%L\\n"
          maeGetOverallSpecStatus(?verbose nil))
        maeCloseResults()
        resultsOpened = nil
        unless(close(resultPort) resultPort = nil
          error("cannot close native Maestro RDB export"))
        resultPort = nil
        printf("FLOW_ISOLATED_MAESTRO_RDB {nonce}\\n")
'''
    return f'''let((session history attempt completed closeAttempt waitStatus
  setupDb resultPort resultDb resultPoints resultPoint resultParam resultOutput
  resultSpecStatus resultExpressionCount resultsOpened)
  session = nil
  history = nil
  completed = nil
  resultPort = nil
  resultsOpened = nil
  envSetVal("asimenv.startup" "projectDir" 'string {skill_quote(str(root))})
  attempt = errset(
    unwindProtect(
      progn(
        session = maeOpenSetup(
          {skill_quote(library)} {skill_quote(cell)} "maestro"
          ?application "Explorer" ?mode "r")
        unless(session error("isolated maeOpenSetup failed"))
{assignments}        history = maeRunSimulation(
          ?session session ?callback {skill_quote(rf'printf("FLOW_ISOLATED_MAESTRO_DONE {nonce}\\n")')})
        unless(history error("isolated maeRunSimulation failed"))
        printf("FLOW_ISOLATED_MAESTRO_STARTED {nonce} %s\\n" history)
        waitStatus = maeWaitUntilDone(history ?session session)
        setupDb = axlGetMainSetupDB(session)
        unless(setupDb error("cannot access isolated Maestro setup database"))
{rdb}        unless(maeCloseSession(
          ?session session ?simulation "wait" ?forceClose nil)
          error("isolated maeCloseSession failed"))
        session = nil
        completed = t)
      progn(
        when(resultsOpened
          closeAttempt = errset(maeCloseResults() nil)
          resultsOpened = nil)
        when(session
          closeAttempt = errset(
            maeCloseSession(?session session ?forceClose t) nil))
        when(resultPort
          closeAttempt = errset(close(resultPort) nil)
          resultPort = nil))
    )
    t)
  unless(completed
    printf("FLOW_ISOLATED_MAESTRO_FAILED {nonce}\\n"))
  exit())
'''


def _virtuoso_executable(resources: Any) -> Path:
    executable = resources.configured_tool(CADENCE_VIRTUOSO_TOOL)
    if executable is None:
        raise FileNotFoundError(
            f"runtime.tools.{CADENCE_VIRTUOSO_TOOL} must name an available executable"
        )
    return executable


def _completion_state(log_text: str, nonce: str) -> tuple[list[str], int, bool]:
    histories = re.findall(
        rf"(?m)^(?:\\o\s+)?FLOW_ISOLATED_MAESTRO_STARTED "
        rf"{re.escape(nonce)} ([A-Za-z0-9_.-]+)\s*$",
        log_text,
    )
    completions = re.findall(
        rf"(?m)^(?:\\o\s+)?FLOW_ISOLATED_MAESTRO_DONE "
        rf"{re.escape(nonce)}\s*$",
        log_text,
    )
    failed = re.search(
        rf"(?m)^(?:\\o\s+)?FLOW_ISOLATED_MAESTRO_FAILED "
        rf"{re.escape(nonce)}\s*$",
        log_text,
    ) is not None
    return histories, len(completions), failed


def run_isolated_maestro(
    client: Any,
    *,
    library: str,
    cell: str,
    variables: Mapping[str, str],
    work_dir: Path,
    worker_log: Path,
    nonce: str,
    timeout: int,
    operation: Any,
    resources: Any,
    result_completion_probe: Callable[[str], bool],
    rdb_export: Path,
) -> IsolatedMaestroRunResult:
    """Run one exact headless worker until its stable simulator result exists."""

    require_workspace_capability(
        operation,
        client,
        authority=WorkspaceAuthority.MAESTRO_SESSION,
        library=library,
        cell=cell,
        view="maestro",
    )
    validate_artifact_component(cell, "Maestro worker testbench")
    validate_artifact_id(nonce, "Maestro worker nonce")
    if timeout <= 0:
        raise ValueError("Maestro worker timeout must be positive")
    absolute_work = Path(os.path.abspath(work_dir))
    absolute_log = Path(os.path.abspath(worker_log))
    if absolute_log.parent != absolute_work:
        raise RuntimeError("Maestro worker log must be a direct work file")
    if absolute_log.exists() or absolute_log.is_symlink():
        raise RuntimeError(f"Maestro worker log path already exists: {absolute_log}")
    absolute_rdb = Path(os.path.abspath(rdb_export))
    if absolute_rdb.parent != absolute_work:
        raise RuntimeError("native Maestro RDB export must be a direct work file")
    if absolute_rdb.exists() or absolute_rdb.is_symlink():
        raise RuntimeError(
            f"native Maestro RDB export path already exists: {absolute_rdb}"
        )

    operation.require_root_identity()
    cds_lib = operation.root / "cds.lib"
    executable = _virtuoso_executable(resources)
    simulation_root = absolute_work / "simulation"
    with (
        resources.owned_tool(CADENCE_VIRTUOSO_TOOL) as owned_launcher,
        owned_input_file(cds_lib) as owned_cds_lib,
        owned_directory(operation.root) as owned_workspace,
        owned_directory(absolute_work) as owned_work,
        owned_directory(simulation_root, create_missing=True) as owned_simulation,
        owned_output_file(owned_work, absolute_log.name) as owned_log,
        owned_output_file(owned_work, "virtuoso.stdout") as owned_stdout,
        owned_output_file(owned_work, "maestro-worker.il") as owned_control,
        owned_output_file(owned_work, "maestro-worker.cds.lib") as owned_canonical_cds,
        owned_atomic_output_file(owned_work, absolute_rdb.name) as owned_rdb,
    ):
        cds_size = os.fstat(owned_cds_lib.fd).st_size
        cds_source = os.pread(owned_cds_lib.fd, cds_size, 0).decode("utf-8")
        control_script = render_isolated_maestro_run_skill(
            library,
            cell,
            variables=variables,
            simulation_root=owned_simulation.child_path,
            nonce=nonce,
            rdb_export=owned_rdb.child_path,
        )
        owned_control.write_bytes(control_script.encode("utf-8"))
        os.fchmod(owned_control.fd, 0o444)
        with ExitStack() as cds_resources:
            canonical_cds, cds_resource_fds = _canonical_worker_cds_lib(
                cds_source,
                workspace=operation.root,
                resources=cds_resources,
            )
            owned_canonical_cds.write_bytes(canonical_cds.encode("utf-8"))
            os.fchmod(owned_canonical_cds.fd, 0o444)
            owned_canonical_cds.require_visible()
            with (
                # Cadence propagates -cdslib to evaluator grandchildren.  A
                # deleted memfd works for the first Virtuoso process but is
                # not a reopenable CLA path there, so expose the exact
                # read-only, watched artifact through its held directory.
                owned_input_file(owned_canonical_cds.path) as owned_worker_cds,
                owned_sealed_input(
                    control_script.encode("utf-8"),
                    name="maestro-worker.il",
                ) as owned_script,
            ):
                xrun = resources.configured_tool("cadence.xrun")
                environment = cadence_ic_env(
                    executable,
                    resources.environment,
                    xrun=xrun,
                )
                command = (
                    *owned_launcher.command,
                    "-nograph",
                    "-nocdsinit",
                    "-cdslib",
                    owned_worker_cds.child_named_path,
                    "-log",
                    owned_log.child_path,
                    "-restore",
                    owned_script.child_path,
                )

                def validate_spawn() -> None:
                    operation.require_root_identity()
                    owned_launcher.require_visible()
                    owned_cds_lib.require_visible()
                    owned_worker_cds.require_visible()
                    owned_script.require_sealed()
                    require_workspace_capability(
                        operation,
                        client,
                        authority=WorkspaceAuthority.MAESTRO_SESSION,
                        library=library,
                        cell=cell,
                        view="maestro",
                    )

                marker_offset = 0
                marker_carry = ""
                marker_lines: list[str] = []

                def completion_confirmed() -> bool:
                    nonlocal marker_offset, marker_carry
                    owned_log.require_visible()
                    size = os.fstat(owned_log.fd).st_size
                    if size < marker_offset:
                        marker_offset = 0
                        marker_carry = ""
                        marker_lines.clear()
                    if size > marker_offset:
                        payload = marker_carry + os.pread(
                            owned_log.fd,
                            size - marker_offset,
                            marker_offset,
                        ).decode("utf-8", errors="replace")
                        marker_offset = size
                        marker_carry = ""
                        for line in payload.splitlines(keepends=True):
                            if not line.endswith(("\n", "\r")):
                                marker_carry = line
                            elif "FLOW_ISOLATED_MAESTRO_" in line:
                                marker_lines.append(line)
                    owned_log.require_visible()
                    histories, callbacks, failed = _completion_state(
                        "".join(marker_lines), nonce
                    )
                    if len(histories) != 1 or callbacks > 1 or failed:
                        return False
                    try:
                        owned_rdb.require_visible()
                        return result_completion_probe(histories[0])
                    except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
                        return False

                try:
                    process_result = run_process_group_until_confirmed(
                        command,
                        executable=owned_launcher.executable,
                        cwd=Path(owned_workspace.child_path),
                        env=environment,
                        timeout=timeout,
                        stdout_fd=owned_stdout.fd,
                        confirmation_probe=completion_confirmed,
                        before_spawn=validate_spawn,
                        pass_fds=(
                            owned_worker_cds.fd,
                            owned_worker_cds.directory_fd,
                            owned_script.fd,
                            owned_cds_lib.fd,
                            owned_workspace.fd,
                            owned_work.fd,
                            owned_simulation.fd,
                            owned_log.fd,
                            owned_stdout.fd,
                            *cds_resource_fds,
                        ),
                    )
                except ProcessGroupCleanupUncertainError as exc:
                    operation.mark_uncertain(
                        f"isolated Maestro worker cleanup could not be proven: {exc}"
                    )
                    raise
        log_text = owned_log.read_bytes().decode("utf-8", errors="replace")
        stdout_text = owned_stdout.read_bytes().decode("utf-8", errors="replace")
        owned_rdb.require_visible()
        if not owned_rdb.read_bytes():
            raise RuntimeError(
                "isolated Maestro worker exported an empty native RDB result"
            )

    try:
        metadata = absolute_log.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError("isolated Virtuoso worker produced no log") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("isolated Virtuoso worker log is not a regular file")
    histories, completion_markers, failed_marker = _completion_state(log_text, nonce)
    completed = process_result.completed
    expected_termination = process_result.leader_terminated_after_confirmation
    result_confirmed = False
    if len(histories) == 1:
        try:
            result_confirmed = result_completion_probe(histories[0])
        except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
            result_confirmed = False
    if (
        (completed.returncode != 0 and not expected_termination)
        or failed_marker
        or completion_markers > 1
        or len(histories) != 1
        or not result_confirmed
    ):
        diagnostics = (log_text + "\n" + stdout_text)[-8000:]
        raise RuntimeError(
            "isolated Virtuoso Maestro worker failed without one started history "
            "and confirmed simulator result\n"
            + diagnostics
        )
    history = histories[0]
    validate_artifact_component(history, "Maestro history")
    return IsolatedMaestroRunResult(
        history=history,
        status="isolated-worker-complete",
        stdout=stdout_text,
        worker_log=absolute_log,
        worker_log_text=log_text,
        control_script=control_script,
        rdb_export=absolute_rdb,
        terminated_after_completion=expected_termination,
    )
