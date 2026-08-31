"""Single-cell schematic import and symbol-generation bridge adapters."""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
import os
import shutil
from typing import Any, Callable

from sigilicon.virtuoso.bridge import schematic_import_netlist_skill
from sigilicon.virtuoso.bridge import generate_symbol_from_schematic
from sigilicon.virtuoso.bridge import escape_skill_string

from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    owned_output_file,
    owned_process_fd_path,
    run_process_group_capture,
)
from sigilicon.paths import validate_artifact_component
from sigilicon.virtuoso.capability import dispatch_oa_mutation, require_oa_target_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    assert_cell_has_no_open_views,
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
)
from sigilicon.virtuoso.workspace import WorkspaceOperation


def check_and_save_schematic(
    client: Any,
    library: str,
    cell: str,
    *,
    timeout: int,
    operation: WorkspaceOperation,
) -> None:
    """Commit current connectivity required by AMS UNL netlisting.

    Cadence reports ``dirty`` when connectivity is current but the schematic
    retains warning markers.  Any other status means the extracted
    connectivity is unusable by a background netlister.
    """

    require_oa_target_capability(
        operation,
        client,
        library=library,
        cell=cell,
        phase="schematic check and save",
    )
    quoted_library = escape_skill_string(library)
    quoted_cell = escape_skill_string(cell)
    source = f'''let((cv attempt status)
  cv = nil
  status = nil
  attempt = errset(
    unwindProtect(
      progn(
        cv = dbOpenCellViewByType("{quoted_library}" "{quoted_cell}" "schematic" "schematic" "a")
        unless(cv error("cannot open imported schematic for check and save"))
        schCheck(cv)
        unless(dbSave(cv) error("imported schematic save failed"))
        status = schExtractStatus(cv)
        unless(member(status list("clean" "dirty"))
          error(sprintf(nil "schematic extraction remained %L" status)))
        t)
      progn(
        when(cv
          unless(dbClose(cv) error("imported schematic close failed"))
          cv = nil)))
    nil)
  unless(attempt && car(attempt)
    error(sprintf(nil "imported schematic check and save failed; extraction status=%L" status)))
  t
)'''
    result = require_bridge_confirmation(
        operation,
        f"check and save imported schematic {library}/{cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=cell,
            phase="schematic check and save SKILL dispatch",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    own_synchronous_cellview_delta_skill(
                        source,
                        label=f"schematic check and save {library}/{cell}",
                    ),
                    label=f"schematic check and save {library}/{cell}",
                    mutation_target=(library, (cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to check and save imported schematic {library}/{cell}: "
            f"{result.errors[0]}"
        )


def _protect_import_conversion_skill(
    source: str,
    *,
    library: str,
    cell: str,
) -> str:
    """Close only handle identities created during one synchronous SKILL call."""

    if "conn2Sch(" not in source:
        return source
    return audit_cellview_delta_skill(
        own_synchronous_cellview_delta_skill(
            _own_import_result_handles(source),
            label="netlist import synchronous conversion",
        ),
        label="netlist import cellview",
        mutation_target=(library, (cell,)),
    )


def _own_import_result_handles(source: str) -> str:
    """Own exact cellview deltas created by synchronous import API calls."""

    scope = "let((vbSchematicObj vbNetlistObj "
    if source.count(scope) != 1:
        raise RuntimeError(
            "virtuoso-bridge import local-variable shape changed; refusing conversion"
        )
    source = source.replace(
        scope,
        scope
        + "vbConnBeforeViews vbConnAfterViews vbConnOwnedViews "
        + "vbConnCloseFailures vbConnCloseAttempt vbConnCv ",
        1,
    )

    def close_delta(
        prefix: str,
        label: str,
        *,
        exclude: str | None = None,
    ) -> str:
        exclusion = "" if exclude is None else f" || equal({prefix}Cv {exclude})"
        return (
            f"{prefix}AfterViews = dbGetOpenCellViews() "
            f"{prefix}OwnedViews = nil "
            f"foreach({prefix}Cv {prefix}AfterViews "
            f"unless(member({prefix}Cv {prefix}BeforeViews){exclusion} "
            f"{prefix}OwnedViews = cons({prefix}Cv {prefix}OwnedViews))) "
            f"{prefix}CloseFailures = nil "
            f"foreach({prefix}Cv {prefix}OwnedViews "
            f"{prefix}CloseAttempt = errset(dbClose({prefix}Cv) t) "
            f"unless({prefix}CloseAttempt && car({prefix}CloseAttempt) "
            f"{prefix}CloseFailures = cons({prefix}Cv {prefix}CloseFailures))) "
            f"when({prefix}CloseFailures "
            f'error(sprintf(nil "{label} exact handle close failed: %L" '
            f"reverse({prefix}CloseFailures)))) "
        )

    conn_call = "vbConnOk = errset(conn2Sch("
    if source.count(conn_call) != 1:
        raise RuntimeError(
            "virtuoso-bridge conn2Sch call shape changed; refusing conversion"
        )
    source = source.replace(
        conn_call,
        "vbConnBeforeViews = dbGetOpenCellViews() "
        + "unwindProtect(progn("
        + conn_call,
        1,
    )

    confirmation = 'unless(vbConnOk && car(vbConnOk) error("conn2Sch failed")) '
    if source.count(confirmation) != 1:
        raise RuntimeError(
            "virtuoso-bridge conn2Sch result shape changed; refusing conversion"
        )
    source = source.replace(
        confirmation,
        ") progn("
        + close_delta("vbConn", "conn2Sch")
        + ")) "
        + confirmation,
        1,
    )
    if "dbCopyCellView(" not in source:
        return _wrap_import_dd_cleanup(source)

    source = source.replace(
        scope
        + "vbConnBeforeViews vbConnAfterViews vbConnOwnedViews "
        + "vbConnCloseFailures vbConnCloseAttempt vbConnCv ",
        scope
        + "vbConnBeforeViews vbConnAfterViews vbConnOwnedViews "
        + "vbConnCloseFailures vbConnCloseAttempt vbConnCv "
        + "vbCopyBeforeViews vbCopyAfterViews vbCopyOwnedViews "
        + "vbCopyCloseFailures vbCopyCloseAttempt vbCopyCv "
        + "vbTempOpenBeforeViews vbTempOpenAfterViews vbTempOpenOwnedViews "
        + "vbTempOpenCloseFailures vbTempOpenCloseAttempt vbTempOpenCv ",
        1,
    )
    temp_open_call = "vbTempCv = dbOpenCellViewByType("
    if source.count(temp_open_call) != 1:
        raise RuntimeError(
            "virtuoso-bridge temporary schematic open shape changed; "
            "refusing conversion"
        )
    source = source.replace(
        temp_open_call,
        "vbTempOpenBeforeViews = dbGetOpenCellViews() "
        + "unwindProtect(progn("
        + temp_open_call,
        1,
    )
    temp_open_confirmation = 'unless(vbTempCv error("temporary schematic open failed")) '
    if source.count(temp_open_confirmation) != 1:
        raise RuntimeError(
            "virtuoso-bridge temporary schematic result shape changed; "
            "refusing conversion"
        )
    source = source.replace(
        temp_open_confirmation,
        ") progn("
        + close_delta(
            "vbTempOpen",
            "temporary schematic open",
            exclude="vbTempCv",
        )
        + ")) "
        + temp_open_confirmation,
        1,
    )
    copy_call = "vbCopyOk = dbCopyCellView("
    if source.count(copy_call) != 1:
        raise RuntimeError(
            "virtuoso-bridge dbCopyCellView call shape changed; refusing conversion"
        )
    source = source.replace(
        copy_call,
        "vbCopyBeforeViews = dbGetOpenCellViews() "
        + "unwindProtect(progn("
        + copy_call,
        1,
    )
    copy_confirmation = 'unless(vbCopyOk error("target schematic copy failed")) '
    if source.count(copy_confirmation) != 1:
        raise RuntimeError(
            "virtuoso-bridge dbCopyCellView result shape changed; refusing conversion"
        )
    source = source.replace(
        copy_confirmation,
        ") progn("
        + close_delta("vbCopy", "dbCopyCellView")
        + ")) "
        + copy_confirmation,
        1,
    )
    cleanup = "progn(when(vbTempCv "
    if source.count(cleanup) != 1:
        raise RuntimeError(
            "virtuoso-bridge dbCopyCellView cleanup shape changed; refusing conversion"
        )
    return _wrap_import_dd_cleanup(source)


def _wrap_import_dd_cleanup(source: str) -> str:
    """Release the exact source-view DD object on every conversion exit."""

    declaration_close = source.find(") ", len("let(("))
    if (
        not source.startswith("let((")
        or declaration_close < 0
        or "vbDestViewName" not in source[:declaration_close]
        or not source.endswith(")")
    ):
        raise RuntimeError(
            "virtuoso-bridge import outer-scope shape changed; refusing conversion"
        )
    source = (
        source[: declaration_close + 2]
        + "unwindProtect(progn("
        + source[declaration_close + 2 :]
    )
    return (
        source
        + " progn(when(vbNetlistObj "
        + "unless(ddReleaseObj(vbNetlistObj) "
        + 'error("source netlist DD handle release failed")) '
        + "vbNetlistObj = nil))))"
    )


def _import_preflight_skill(
    library: str,
    cell: str,
    *,
    netlist_view: str,
    schematic_view: str,
    overwrite: bool,
) -> str:
    """Check import conflicts while reliably releasing every dd handle."""

    escaped_library = escape_skill_string(library)
    escaped_cell = escape_skill_string(cell)
    escaped_netlist_view = escape_skill_string(netlist_view)
    escaped_schematic_view = escape_skill_string(schematic_view)
    allow_overwrite = "t" if overwrite else "nil"
    return (
        "let((flowSchematicObj flowNetlistObj) "
        "unwindProtect("
        "progn("
        f'when("{escaped_netlist_view}" == "{escaped_schematic_view}" '
        'error("netlist and schematic views must differ")) '
        f'flowSchematicObj = ddGetObj("{escaped_library}" "{escaped_cell}" '
        f'"{escaped_schematic_view}") '
        f"when(flowSchematicObj && !{allow_overwrite} "
        'error("target schematic exists")) '
        f'flowNetlistObj = ddGetObj("{escaped_library}" "{escaped_cell}" '
        f'"{escaped_netlist_view}") '
        f"when(flowNetlistObj && !{allow_overwrite} "
        'error("target netlist exists")) '
        "t) "
        "progn("
        "when(flowNetlistObj ddReleaseObj(flowNetlistObj) "
        "flowNetlistObj = nil) "
        "when(flowSchematicObj ddReleaseObj(flowSchematicObj) "
        "flowSchematicObj = nil))))"
    )


def _require_skill_result(result: Any, label: str) -> Any:
    if result.errors:
        raise RuntimeError(f"{label}: {result.errors[0]}")
    if result.ok is not True:
        raise RuntimeError(f"{label}: unconfirmed bridge status {result.status!r}")
    return result


class _ImportCleanupClient:
    """Forward a bridge client while hardening its conn2Sch conversion call."""

    def __init__(
        self,
        client: Any,
        *,
        operation: WorkspaceOperation,
        library: str,
        cell: str,
    ) -> None:
        self._client = client
        self._operation = operation
        self._library = library
        self._cell = cell

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def execute_skill(self, source: str, **kwargs: Any) -> Any:
        if "conn2Sch(" in source:
            self._operation.require_active_mutation(
                self._client,
                self._library,
                self._cell,
                phase="conn2Sch conversion",
            )
        return self._client.execute_skill(
            _protect_import_conversion_skill(
                source,
                library=self._library,
                cell=self._cell,
            ),
            **kwargs,
        )


class _SymbolCleanupClient:
    """Scope native symbol generation and all implicit master-view opens."""

    def __init__(
        self,
        client: Any,
        *,
        operation: WorkspaceOperation,
        library: str,
        cell: str,
    ) -> None:
        self._client = client
        self._operation = operation
        self._library = library
        self._cell = cell
        self._label = f"symbol generation {library}/{cell}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def execute_skill(self, source: str, **kwargs: Any) -> Any:
        self._operation.require_active_mutation(
            self._client,
            self._library,
            self._cell,
            phase="symbol generator SKILL dispatch",
        )
        return self._client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    source,
                    label=self._label,
                ),
                label=self._label,
                mutation_target=(self._library, (self._cell,)),
            ),
            **kwargs,
        )


def _import_netlist(
    client: Any,
    library: str,
    cell: str,
    netlist: Path,
    *,
    operation: WorkspaceOperation,
    own_netlist: Callable[[], AbstractContextManager[int]],
    **kwargs: Any,
) -> Any:
    """Run one fully fd-owned local spiceIn import and conn2Sch conversion."""

    if getattr(client, "ssh_runner", None) is not None:
        raise RuntimeError(
            "remote spiceIn is unsupported because exact process-group and fd "
            "ownership cannot be proven"
        )
    if kwargs.get("run_dir") is None:
        raise ValueError("spiceIn requires an owned artifact run directory")
    run_dir = Path(os.path.abspath(Path(str(kwargs["run_dir"]))))
    dev_map_file = kwargs.get("dev_map_file")
    language = str(kwargs.get("language", "Spectre"))
    sim_name = str(kwargs.get("sim_name", "spectre"))
    output_sim_name = str(kwargs.get("output_sim_name", "spectre"))
    netlist_view = str(kwargs.get("netlist_view", "netlist"))
    schematic_view = str(kwargs.get("schematic_view", "schematic"))
    reference_libraries = tuple(
        name
        for name in dict.fromkeys(
            str(item) for item in kwargs.get("ref_libs", ("analogLib", "basic"))
        )
        if name != library
    )
    cds_libraries = tuple(dict.fromkeys((library, *reference_libraries)))
    for name in cds_libraries:
        validate_artifact_component(name, "Cadence library")
    overwrite = bool(kwargs.get("overwrite", False))
    timeout = int(kwargs.get("timeout", 300))

    cds_home = os.environ.get("CDSHOME")
    candidates = [Path(found) for found in [shutil.which("spiceIn")] if found]
    if cds_home:
        candidates.extend(
            (
                Path(cds_home) / "tools" / "dfII" / "bin" / "spiceIn",
                Path(cds_home) / "bin" / "spiceIn",
            )
        )
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise FileNotFoundError("spiceIn was not found in PATH or CDSHOME")

    operation.require_active_mutation(
        client,
        library,
        cell,
        phase="spiceIn target-view preflight",
    )
    _require_skill_result(
        client.execute_skill(
            _import_preflight_skill(
                library,
                cell,
                netlist_view=netlist_view,
                schematic_view=schematic_view,
                overwrite=overwrite,
            ),
            timeout=timeout,
        ),
        "netlist import preflight failed",
    )

    def parameter_payload(
        *,
        netlist_value: str,
        dev_map_value: str,
        log_value: str,
    ) -> bytes:
        rows = [
            "spiceInParams = list(nil",
            f'  \'language "{escape_skill_string(language)}"',
            f'  \'netlistFile "{escape_skill_string(netlist_value)}"',
            f'  \'importSubList "{escape_skill_string(cell)}"',
            f'  \'outputLib "{escape_skill_string(library)}"',
            f'  \'refLibList "{escape_skill_string(" ".join(reference_libraries))}"',
            f'  \'outputViewName "{escape_skill_string(netlist_view)}"',
            '  \'outputViewType "netlist"',
            f'  \'simName "{escape_skill_string(sim_name)}"',
            f'  \'outputSimName "{escape_skill_string(output_sim_name)}"',
            f'  \'overwriteCells "{"all" if overwrite else "none"}"',
            f'  \'devMapFile "{escape_skill_string(dev_map_value)}"',
            '  \'masterCellForGnd "gnd"',
            f'  \'logFile "{escape_skill_string(log_value)}"',
            ")",
            "",
        ]
        return "\n".join(rows).encode("utf-8")

    with (
        owned_directory(run_dir, create_missing=True) as owned_run_dir,
        owned_output_file(owned_run_dir, "spiceIn.il") as owned_parameter,
        owned_output_file(owned_run_dir, "cds.lib") as owned_staged_cds,
        owned_output_file(owned_run_dir, "spiceIn.log") as owned_spicein_log,
        owned_output_file(owned_run_dir, "spiceIn.stdout") as owned_stdout,
        ExitStack() as inputs,
    ):
        netlist_fd = inputs.enter_context(own_netlist())
        owned_dev_map = (
            None
            if dev_map_file is None
            else inputs.enter_context(owned_input_file(Path(str(dev_map_file))))
        )
        owned_libraries = []
        for name in cds_libraries:
            info = client.library.get(name, timeout=timeout)
            raw_path = Path(str(info.path))
            library_path = Path(
                os.path.abspath(
                    raw_path if raw_path.is_absolute() else operation.root / raw_path
                )
            )
            owned_libraries.append(
                (
                    name,
                    inputs.enter_context(
                        owned_directory(library_path, create_missing=False)
                    ),
                )
            )
        owned_parameter.write_bytes(
            parameter_payload(
                netlist_value=owned_process_fd_path(netlist_fd),
                dev_map_value=(
                    "" if owned_dev_map is None else owned_dev_map.child_path
                ),
                log_value=owned_spicein_log.child_path,
            )
        )
        owned_staged_cds.write_bytes(
            (
                "\n".join(
                    f"DEFINE {name} {owned_library.child_path}"
                    for name, owned_library in owned_libraries
                )
                + "\n"
            ).encode("utf-8")
        )
        os.fchmod(owned_parameter.fd, 0o444)
        os.fchmod(owned_staged_cds.fd, 0o444)
        pass_fds = [
            netlist_fd,
            owned_parameter.fd,
            owned_staged_cds.fd,
            owned_spicein_log.fd,
            owned_run_dir.fd,
            *(owned_library.fd for _, owned_library in owned_libraries),
        ]
        if owned_dev_map is not None:
            pass_fds.append(owned_dev_map.fd)
        command = (
            str(executable),
            "-param",
            owned_parameter.child_path,
        )
        completed = run_process_group_capture(
            command,
            cwd=Path(owned_run_dir.child_path),
            env=cadence_subprocess_env(),
            timeout=timeout,
            before_spawn=lambda: operation.require_active_mutation(
                client,
                library,
                cell,
                phase="spiceIn process launch",
            ),
            pass_fds=tuple(pass_fds),
        )
        owned_stdout.write_bytes(
            ((completed.stdout or "") + (completed.stderr or "")).encode(
                "utf-8", errors="replace"
            )
        )
        if completed.returncode != 0:
            tail = owned_spicein_log.read_bytes().decode(
                "utf-8", errors="replace"
            )[-6000:]
            raise RuntimeError(
                f"spiceIn failed for {library}/{cell}/{netlist_view}\n{tail}"
            )
        conversion = schematic_import_netlist_skill(
            library,
            cell,
            netlist_view=netlist_view,
            schematic_view=schematic_view,
            overwrite=overwrite,
            param_file=run_dir / "spiceIn.il",
            spicein_log_file=run_dir / "spiceIn.log",
        )
        result = _ImportCleanupClient(
            client,
            operation=operation,
            library=library,
            cell=cell,
        ).execute_skill(conversion, timeout=timeout)
        return _require_skill_result(result, "netlist import conversion failed")
def _generate_symbol(
    client: Any,
    library: str,
    cell: str,
    *,
    sort_pins: str,
    overwrite: bool,
    timeout: int,
    operation: WorkspaceOperation,
) -> Any:
    return generate_symbol_from_schematic(
        _SymbolCleanupClient(
            client,
            operation=operation,
            library=library,
            cell=cell,
        ),
        library,
        cell,
        sort_pins=sort_pins,
        overwrite=overwrite,
        timeout=timeout,
    )


def import_schematic(
    client: Any,
    library: str,
    cell: str,
    netlist: Path,
    *,
    own_netlist: Callable[[], AbstractContextManager[int]],
    reference_libraries: tuple[str, ...],
    dev_map_file: Path | None,
    overwrite: bool,
    run_dir: Path | None,
    timeout: int,
    operation: WorkspaceOperation,
) -> Any:
    """Import exactly one named subcircuit as one OA schematic."""

    require_oa_target_capability(
        operation,
        client,
        library=library,
        cell=cell,
        phase="schematic import",
    )
    kwargs: dict[str, object] = {
        "language": "Spectre",
        "ref_libs": reference_libraries,
        "overwrite": overwrite,
        "timeout": timeout,
    }
    if run_dir is not None:
        kwargs["run_dir"] = run_dir
    if dev_map_file is not None:
        kwargs["dev_map_file"] = dev_map_file
    result = require_bridge_confirmation(
        operation,
        f"import schematic {library}/{cell}",
        lambda: _import_netlist(
            client,
            library,
            cell,
            netlist,
            operation=operation,
            own_netlist=own_netlist,
            **kwargs,
        ),
    )
    check_and_save_schematic(
        client,
        library,
        cell,
        timeout=timeout,
        operation=operation,
    )
    require_bridge_confirmation(
        operation,
        f"verify schematic import cleanup {library}/{cell}",
        lambda: assert_cell_has_no_open_views(client, library, cell),
    )
    return result


def generate_symbol(
    client: Any,
    library: str,
    cell: str,
    *,
    sort_pins: str,
    overwrite: bool,
    timeout: int,
    operation: WorkspaceOperation,
) -> Any:
    """Generate exactly one symbol from one existing schematic."""

    require_oa_target_capability(
        operation,
        client,
        library=library,
        cell=cell,
        phase="symbol generation",
    )
    result = require_bridge_confirmation(
        operation,
        f"generate symbol {library}/{cell}",
        lambda: _generate_symbol(
            client,
            library,
            cell,
            sort_pins=sort_pins,
            overwrite=overwrite,
            timeout=timeout,
            operation=operation,
        ),
    )
    require_bridge_confirmation(
        operation,
        f"verify symbol cleanup {library}/{cell}",
        lambda: assert_cell_has_no_open_views(client, library, cell),
    )
    return result
