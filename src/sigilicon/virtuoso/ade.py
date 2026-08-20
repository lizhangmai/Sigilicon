"""Cadence ADE/config adapter; all bridge Maestro calls terminate here."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import uuid

from sigilicon.virtuoso.bridge import escape_skill_string

from sigilicon.virtuoso.capability import dispatch_oa_mutation
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.maestro import build_owned_maestro_setup_transaction_skill
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
    skill_quote,
)


def build_ie_cards(
    *,
    vdd: float,
    connect_rules: str,
    rise_time: str,
    vthi: float | None = None,
    vtlo: float | None = None,
) -> str:
    """Encode the shared interface-element contract for ADE AMS."""

    if (vthi is None) != (vtlo is None):
        raise ValueError("AMS interface thresholds must be both present or both absent")
    escaped_rules = escape_skill_string(connect_rules)
    parameter_text = f"discipline=logic;tr={rise_time};tf={rise_time};"
    if vthi is not None and vtlo is not None:
        parameter_text += f"vthi={vthi:g};vtlo={vtlo:g};"
    parameters = escape_skill_string(parameter_text)
    return (
        f'((t "global" "" "Value" "{vdd:g}" '
        f'"connectLib.CR_{escaped_rules}_fast" "" "logic" '
        f'"{parameters}" "Built-in"))'
    )


def create_config_view(
    client: Any,
    *,
    library: str,
    testbench: str,
    dut: str,
    reference_libraries: tuple[str, ...],
    operation: Any,
) -> None:
    libraries = tuple(
        dict.fromkeys((library, *reference_libraries, "analogLib", "basic"))
    )
    source = f'''let((cfg ok attempt)
  cfg = nil
  attempt = errset(
    unwindProtect(
      progn(
        cfg = hdbOpen({skill_quote(library)} {skill_quote(testbench)} "config" "w" "CDBA")
        unless(cfg error("cannot open config"))
        ok = hdbSetTopCellViewName(cfg {skill_quote(library)} {skill_quote(testbench)} "systemVerilog")
        unless(ok error("set top failed"))
        hdbSetDefaultLibListString(cfg {skill_quote(' '.join(libraries))})
        hdbSetDefaultViewListString(cfg "spectre spice systemVerilog schematic veriloga behavioral symbol")
        hdbSetDefaultStopListString(cfg "spectre")
        ok = hdbSetObjBindRule(cfg list(list({skill_quote(library)} {skill_quote(dut)} nil nil)) list('hdbcBindingRule list({skill_quote(library)} {skill_quote(dut)} "schematic")))
        unless(ok error("bind DUT failed"))
        unless(hdbSave(cfg) error("config save failed"))
        t
      )
      when(cfg unless(hdbClose(cfg) error("config close failed")) cfg = nil)
    )
    nil
  )
  unless(attempt && car(attempt) error("config creation failed"))
  t
)'''
    result = require_bridge_confirmation(
        operation,
        f"create config {library}/{testbench}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=testbench,
            phase="create config view SKILL dispatch",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    own_synchronous_cellview_delta_skill(
                        source,
                        label=f"config creation {library}/{testbench}",
                    ),
                    label=f"config creation {library}/{testbench}",
                    mutation_target=(library, (testbench,)),
                ),
                timeout=180,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create {library}/{testbench}/config: {result.errors[0]}"
        )


def create_maestro_view(
    client: Any,
    *,
    library: str,
    testbench: str,
    signals: tuple[str, ...],
    stop: str,
    maxstep: str,
    errpreset: str,
    model_file: Path,
    model_section: str,
    vdd: float,
    connect_rules: str,
    rise_time: str,
    vthi: float,
    vtlo: float,
    operation: Any,
) -> None:
    output_actions = "\n".join(
        f'''        maeAddOutput({skill_quote(name)} "TRAN"
          ?outputType "net" ?signalName {skill_quote(f"/{name}")}
          ?session session)'''
        for name in signals
    )
    ie_cards = build_ie_cards(
        vdd=vdd,
        connect_rules=connect_rules,
        rise_time=rise_time,
        vthi=vthi,
        vtlo=vtlo,
    )
    scope_token = uuid.uuid4().hex
    operation.record_ownership_scope(
        "maestro-setup",
        scope_token=scope_token,
        library=library,
        cell=testbench,
    )
    body = f'''      maeCreateTest("TRAN" ?lib {skill_quote(library)}
        ?cell {skill_quote(testbench)} ?view "config"
        ?simulator "ams" ?session session)
      toolSession = axlGetToolSession(session "TRAN")
      unless(toolSession error("No ADE tool session"))
      sessionType = sevGetSessionType(toolSession)
      unless(equal(sessionType "explorer")
        error(sprintf(nil "Expected ADE Explorer session, got %L" sessionType)))
      maeSetAnalysis("TRAN" "tran" ?enable t
        ?options `(("stop" {skill_quote(stop)})
          ("errpreset" {skill_quote(errpreset)})
          ("maxstep" {skill_quote(maxstep)}))
        ?session session)
{output_actions}
      maeSetEnvOption("TRAN"
        ?options `(("modelFiles"
          (({skill_quote(str(model_file))} {skill_quote(model_section)}))))
        ?session session)
      maeSetEnvOption("TRAN"
        ?options `(("useIeSetup" t) ("ieUseUcmAsDefault" t)
          ("amsIEsList" {ie_cards}))
        ?session session)
      ok = maeSaveSetup(?lib {skill_quote(library)}
        ?cell {skill_quote(testbench)} ?view "maestro" ?session session)
      unless(ok error("maeSaveSetup did not confirm persistence"))'''
    source = own_synchronous_cellview_delta_skill(
        build_owned_maestro_setup_transaction_skill(
            library,
            testbench,
            scope_token=scope_token,
            body=body,
        ),
        label=f"Maestro setup {library}/{testbench}",
    )
    result = require_bridge_confirmation(
        operation,
        f"create Maestro setup {library}/{testbench}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=testbench,
            phase="create Maestro view SKILL dispatch",
            callback=lambda: client.execute_skill(source, timeout=300),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create {library}/{testbench}/maestro: {result.errors[0]}"
        )


def _load_native_setup_skill(source: Path, invocation: str) -> str:
    """Load one source-owned SKILL setup and invoke a named entry point."""

    if not source.is_file():
        raise ValueError(f"native ADE setup source does not exist: {source}")
    return f'''let((loaded invoked)
  loaded = errset(load({skill_quote(str(source))}) nil)
  unless(loaded && car(loaded)
    error("native ADE setup source load failed"))
  invoked = errset({invocation} nil)
  unless(invoked && car(invoked)
    error(sprintf(nil "native ADE setup entry point failed: %L" invoked)))
  t
)'''


def _native_setup_entry_point(
    source: Path,
    *,
    generic: str,
) -> str:
    """Resolve the one source-owned native setup entry point."""

    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read native ADE setup source: {source}") from exc
    native_names = tuple(
        dict.fromkeys(
            re.findall(
                rf"\bprocedure\(({re.escape(generic)}[A-Za-z0-9_$]*)\s*\(",
                text,
            )
        )
    )
    if len(native_names) != 1:
        raise ValueError(
            "native ADE setup must define one source-owned entry point starting "
            f"with {generic}: {source}"
        )
    return native_names[0]


def _native_setup_test_name(source: Path) -> str:
    """Extract the one source-owned test identity used by a native setup."""

    text = source.read_text(encoding="utf-8")
    names = tuple(
        re.findall(r"\bmaeCreateTest\(\s*\"([A-Za-z_][A-Za-z0-9_$]*)\"", text)
    )
    if len(names) != 1:
        raise ValueError(
            "native Maestro setup must declare exactly one maeCreateTest identity: "
            f"{source}"
        )
    return names[0]


def create_oa_native_config_view(
    client: Any,
    spec: Any,
    *,
    reference_libraries: tuple[str, ...],
    operation: Any,
    timeout: int = 180,
) -> None:
    """Materialize a config from the canonical Cadence SKILL setup source."""

    native_setup = spec.native_setup
    if native_setup is None:
        raise ValueError("native config materialization requires schema-3 setup")
    references = " ".join(reference_libraries)
    entry_point = _native_setup_entry_point(
        native_setup.source,
        generic="llmCimNativeConfig",
    )
    invocation = (
        f"{entry_point}({skill_quote(spec.library)} "
        f"{skill_quote(spec.cell)} {skill_quote(spec.dut)} "
        f"{skill_quote(spec.top_view)} {skill_quote(references)})"
    )
    source = _load_native_setup_skill(native_setup.source, invocation)
    source = own_synchronous_cellview_delta_skill(
        source,
        label=f"native config setup {spec.library}/{spec.cell}",
    )
    result = require_bridge_confirmation(
        operation,
        f"create native config {spec.library}/{spec.cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=spec.library,
            cell=spec.cell,
            phase="create native config view SKILL dispatch",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    source,
                    label=f"native config setup {spec.library}/{spec.cell}",
                    mutation_target=(spec.library, (spec.cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create native config {spec.library}/{spec.cell}: "
            f"{result.errors[0]}"
        )


def create_oa_native_maestro_view(
    client: Any,
    spec: Any,
    *,
    operation: Any,
    timeout: int = 300,
) -> None:
    """Materialize a Maestro setup by executing the canonical SKILL source."""

    native_setup = spec.native_setup
    if native_setup is None:
        raise ValueError("native Maestro materialization requires schema-3 setup")
    scope_token = uuid.uuid4().hex
    operation.record_ownership_scope(
        "maestro-setup",
        scope_token=scope_token,
        library=spec.library,
        cell=spec.cell,
    )
    entry_point = _native_setup_entry_point(
        native_setup.source,
        generic="llmCimNativeMaestro",
    )
    canonical_test = _native_setup_test_name(native_setup.source)
    invocation = (
        f"{entry_point}(session {skill_quote(spec.library)} "
        f"{skill_quote(spec.cell)} {skill_quote(str(native_setup.pdk.model_file))} "
        f"{skill_quote(native_setup.pdk.model_section)})"
    )
    body = _load_native_setup_skill(native_setup.source, invocation)
    source = own_synchronous_cellview_delta_skill(
        build_owned_maestro_setup_transaction_skill(
            spec.library,
            spec.cell,
            scope_token=scope_token,
            body=body,
            canonical_test=canonical_test,
        ),
        label=f"native Maestro setup {spec.library}/{spec.cell}",
    )
    result = require_bridge_confirmation(
        operation,
        f"create native Maestro setup {spec.library}/{spec.cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=spec.library,
            cell=spec.cell,
            phase="create native Maestro view SKILL dispatch",
            callback=lambda: client.execute_skill(source, timeout=timeout),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to create native Maestro {spec.library}/{spec.cell}: "
            f"{result.errors[0]}"
        )


def export_oa_maestro_setup(
    client: Any,
    *,
    library: str,
    cell: str,
    script_path: Path,
    setup_path: Path,
    operation: Any,
    timeout: int = 180,
) -> None:
    """Export official Maestro script and setup-DB artifacts read-only."""

    project_root = Path(operation.root).resolve().parent
    for path in (script_path, setup_path):
        if path.is_dir():
            raise ValueError(f"Maestro capture output must be a file: {path}")
        if not Path(path).resolve().is_relative_to(project_root):
            raise ValueError("Maestro capture output must stay inside the project")
        if path.exists() or path.is_symlink():
            raise ValueError(f"Maestro capture output already exists: {path}")
    source = f'''let((session setupDb scriptOk setupOk closeOk attempt)
  session = nil
  attempt = errset(
    unwindProtect(
      progn(
        session = maeOpenSetup({skill_quote(library)} {skill_quote(cell)}
          "maestro" ?application "Explorer" ?mode "r")
        unless(session error("cannot open Maestro setup read-only"))
        setupDb = axlGetMainSetupDB(session)
        unless(setupDb error("cannot access Maestro setup DB"))
        scriptOk = maeWriteScript({skill_quote(str(script_path))}
          ?session session ?shouldRunActive nil)
        setupOk = axlExportSetup(
          session setupDb {skill_quote(str(setup_path))}
          (list "vars" "tests" "parameters" "corners" "runoptions" "scripts"))
        closeOk = maeCloseSession(?session session ?forceClose nil)
        session = nil
        unless(scriptOk error("maeWriteScript failed"))
        unless(setupOk error("axlExportSetup failed"))
        unless(closeOk error("Maestro read-only close failed"))
        t)
      when(session errset(maeCloseSession(?session session ?forceClose t) nil)))
    nil)
  unless(attempt && car(attempt)
    error("Maestro official setup export failed"))
  t)'''
    result = require_bridge_confirmation(
        operation,
        f"export Maestro setup {library}/{cell}",
        lambda: client.execute_skill(
            own_synchronous_cellview_delta_skill(
                source,
                label=f"Maestro setup export {library}/{cell}",
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to export Maestro setup {library}/{cell}: {result.errors[0]}"
        )
