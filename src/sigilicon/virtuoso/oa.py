"""Narrow OA/SKILL adapter for project-owned lifecycle safety."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from sigilicon.virtuoso.bridge import decode_skill_output
from sigilicon.virtuoso.bridge import skill_quote

from sigilicon.virtuoso.capability import (
    WorkspaceAuthority,
    dispatch_oa_mutation,
    require_workspace_capability,
)
from sigilicon.virtuoso.confirmation import require_bridge_confirmation


_ENGINEERING_LITERAL = re.compile(
    r"(?P<number>[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)"
    r"(?P<suffix>meg|[afpnumkgt])?\Z",
    re.IGNORECASE,
)
_ENGINEERING_SCALE = {
    "a": Decimal("1e-18"),
    "f": Decimal("1e-15"),
    "p": Decimal("1e-12"),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "meg": Decimal("1e6"),
    "g": Decimal("1e9"),
    "t": Decimal("1e12"),
}
_PARAMETER_REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_SCHEMATIC_PIN_MASTER_CELLS = frozenset({"ipin", "opin", "iopin"})


@dataclass(frozen=True, order=True)
class OpenCellViewInfo:
    library: str
    cell: str
    view: str
    mode: str
    visible: bool
    identity: str = ""


@dataclass(frozen=True)
class WindowCloseResult:
    closed: int
    remaining: int


def own_synchronous_cellview_delta_skill(source: str, *, label: str) -> str:
    """Close exact hidden dbIds created during one synchronous SKILL request.

    Virtuoso executes one submitted SKILL expression on its main thread.  This
    scope therefore owns only identities absent immediately before the body and
    present in its ``unwindProtect`` cleanup.  Visible window handles are never
    closed, and every attempted close is verified against the live open set.
    Cadence PCells can pin read-only cache masters after ``dbClose``; only an
    exact, hidden, still-open read handle may use ``dbPurge`` as a final cleanup.
    """

    if not source.strip():
        raise ValueError("synchronous cellview scope requires non-empty SKILL")
    return f'''let((flowSyncBefore flowSyncBodyResult flowSyncAfter
  flowSyncOwned flowSyncFailures flowSyncCv flowSyncVisible
  flowSyncCloseAttempt flowSyncPurgeAttempt flowSyncWindows flowSyncWindow)
  flowSyncBefore = dbGetOpenCellViews()
  flowSyncBodyResult = unwindProtect(
    progn(
{source}
    )
    progn(
      flowSyncAfter = dbGetOpenCellViews()
      flowSyncOwned = nil
      foreach(flowSyncCv flowSyncAfter
        unless(member(flowSyncCv flowSyncBefore)
          flowSyncOwned = cons(flowSyncCv flowSyncOwned)))
      flowSyncWindows = hiGetWindowList()
      flowSyncFailures = nil
      foreach(flowSyncCv flowSyncOwned
        flowSyncVisible = nil
        foreach(flowSyncWindow flowSyncWindows
          when(equal(geGetWindowCellView(flowSyncWindow) flowSyncCv)
            flowSyncVisible = t))
        if(flowSyncVisible
          then
            flowSyncFailures = cons(flowSyncCv flowSyncFailures)
          else
            flowSyncCloseAttempt = errset(dbClose(flowSyncCv) t)
            when(!flowSyncCloseAttempt ||
              member(flowSyncCv dbGetOpenCellViews())
              flowSyncPurgeAttempt = nil
              when(member(flowSyncCv dbGetOpenCellViews()) &&
                equal(flowSyncCv~>mode "r")
                flowSyncPurgeAttempt = errset(dbPurge(flowSyncCv) t))
              when(!flowSyncPurgeAttempt ||
                member(flowSyncCv dbGetOpenCellViews())
                flowSyncFailures = cons(flowSyncCv flowSyncFailures)))))
      when(flowSyncFailures
        error(sprintf(nil "%s exact synchronous handle cleanup failed: %L"
          {skill_quote(label)} reverse(flowSyncFailures)))))
  )
  flowSyncBodyResult
)'''


def _owned_db_open_cellview_skill(
    *,
    library: str,
    cell: str,
    view_expression: str,
    view_type: str,
    mode: str,
    result_variable: str,
    label: str,
) -> str:
    """Open one target and close only implicit dbIds from that synchronous call."""

    return f'''let((flowOpenBefore flowOpenAfter flowOpenOwned
  flowOpenFailures flowOpenCv flowOpenAttempt flowCloseAttempt flowPurgeAttempt)
  flowOpenBefore = dbGetOpenCellViews()
  when(setof(flowOpenCv flowOpenBefore
    equal(flowOpenCv~>libName {skill_quote(library)}) &&
    equal(flowOpenCv~>cellName {skill_quote(cell)}) &&
    equal(flowOpenCv~>viewName {view_expression}))
    error(sprintf(nil "target became busy before exact open for %s" {skill_quote(label)})))
  flowOpenAttempt = nil
  unwindProtect(
    progn(
      flowOpenAttempt = errset(
        dbOpenCellViewByType({skill_quote(library)} {skill_quote(cell)}
          {view_expression} {skill_quote(view_type)} {skill_quote(mode)})
        t)
      when(flowOpenAttempt
        {result_variable} = car(flowOpenAttempt)))
    progn(
      flowOpenAfter = dbGetOpenCellViews()
      flowOpenOwned = nil
      foreach(flowOpenCv flowOpenAfter
        unless(member(flowOpenCv flowOpenBefore) ||
          equal(flowOpenCv {result_variable})
          flowOpenOwned = cons(flowOpenCv flowOpenOwned)))
      flowOpenFailures = nil
      foreach(flowOpenCv flowOpenOwned
        flowCloseAttempt = errset(dbClose(flowOpenCv) t)
        when(!flowCloseAttempt || member(flowOpenCv dbGetOpenCellViews())
          flowPurgeAttempt = nil
          when(member(flowOpenCv dbGetOpenCellViews()) &&
            equal(flowOpenCv~>mode "r")
            flowPurgeAttempt = errset(dbPurge(flowOpenCv) t))
          when(!flowPurgeAttempt || member(flowOpenCv dbGetOpenCellViews())
            flowOpenFailures = cons(flowOpenCv flowOpenFailures))))
      when(flowOpenFailures
        error(sprintf(nil "%s implicit handle close failed: %L"
          {skill_quote(label)} reverse(flowOpenFailures)))))
))'''


def audit_cellview_delta_skill(
    source: str,
    *,
    label: str,
    mutation_target: tuple[str, tuple[str, ...]] | None = None,
) -> str:
    """Audit one request without claiming snapshot-delta handles as owned.

    The wrapped implementation must close every handle it explicitly acquired
    with its own ``unwindProtect``.  A process-wide delta may be a concurrently
    opened user view, so residual dbIds are preserved and reported rather than
    closed by inference.
    """

    if not source.strip():
        raise ValueError("cellview handle scope requires non-empty SKILL")
    target_guard = ""
    guard_local = ""
    if mutation_target is not None:
        library, cells = mutation_target
        if not cells or len(set(cells)) != len(cells):
            raise ValueError("mutation target cells must be non-empty and unique")
        quoted_cells = " ".join(skill_quote(cell) for cell in cells)
        guard_local = " flowTargetConflicts"
        target_guard = f'''  flowTargetConflicts = setof(flowCv flowBeforeViews
    equal(flowCv~>libName {skill_quote(library)}) &&
    member(flowCv~>cellName list({quoted_cells})))
  when(flowTargetConflicts
    error(sprintf(nil
      "target became busy before atomic SKILL dispatch for %s: %L"
      {skill_quote(label)} flowTargetConflicts)))
'''
    return f'''let((flowBeforeViews flowBodyResult flowAfterViews
  flowUnownedViews{guard_local})
  flowBeforeViews = dbGetOpenCellViews()
{target_guard}  flowBodyResult = unwindProtect(
    progn(
{source}
    )
    progn(
      flowAfterViews = dbGetOpenCellViews()
      flowUnownedViews = nil
      foreach(flowCv flowAfterViews
        unless(member(flowCv flowBeforeViews)
          flowUnownedViews = cons(flowCv flowUnownedViews)))
      when(flowUnownedViews
        error(sprintf(nil "%s left unowned cellviews; preserved exact dbIds: %L"
          {skill_quote(label)} reverse(flowUnownedViews)))))
  )
  flowBodyResult
)'''


def virtuoso_pid(client: Any) -> int:
    result = client.execute_skill("ipcGetPid()", timeout=20)
    if result.errors:
        raise RuntimeError(result.errors[0])
    raw = (result.output or "").strip()
    try:
        pid = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Virtuoso returned an invalid process ID: {raw!r}") from exc
    if pid <= 0:
        raise RuntimeError(f"Virtuoso returned an invalid process ID: {pid}")
    return pid


def cell_exists(client: Any, library: str, cell: str) -> bool:
    result = client.execute_skill(
        f"ddGetObj({skill_quote(library)} {skill_quote(cell)})",
        timeout=20,
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    return (result.output or "").strip() not in {"", "nil", '"nil"'}


def cell_view_exists(client: Any, library: str, cell: str, view: str) -> bool:
    result = client.execute_skill(
        f"ddGetObj({skill_quote(library)} {skill_quote(cell)} {skill_quote(view)})",
        timeout=20,
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    return (result.output or "").strip() not in {"", "nil", '"nil"'}


def delete_cell(
    client: Any,
    library: str,
    cell: str,
    *,
    operation: Any,
    timeout: int = 120,
) -> None:
    """Delete one exact, quiescent project OA cell through ``ddDeleteObj``."""

    source = f'''let((obj deleted)
  obj = ddGetObj({skill_quote(library)} {skill_quote(cell)})
  unless(obj error("OA deletion target does not exist"))
  deleted = ddDeleteObj(obj)
  unless(deleted error("OA cell delete failed"))
  when(ddGetObj({skill_quote(library)} {skill_quote(cell)})
    error("OA cell remained registered after delete"))
  t
)'''
    result = require_bridge_confirmation(
        operation,
        f"delete OA cell {library}/{cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=cell,
            phase=f"delete OA cell {library}/{cell}",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    own_synchronous_cellview_delta_skill(
                        source,
                        label=f"delete OA cell {library}/{cell}",
                    ),
                    label=f"delete OA cell {library}/{cell}",
                    mutation_target=(library, (cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to delete OA cell {library}/{cell}: {result.errors[0]}"
        )
    if decode_skill_output(result.output or "").strip() != "t":
        raise RuntimeError(
            f"unexpected OA deletion result for {library}/{cell}: {result.output!r}"
        )


def delete_cell_view(
    client: Any,
    library: str,
    cell: str,
    view: str,
    *,
    operation: Any,
    timeout: int = 120,
) -> None:
    """Delete one exact, quiescent project OA view while retaining its cell."""

    source = f'''let((cellObj viewObj deleted)
  cellObj = ddGetObj({skill_quote(library)} {skill_quote(cell)})
  unless(cellObj error("OA view parent cell does not exist"))
  viewObj = ddGetObj({skill_quote(library)} {skill_quote(cell)} {skill_quote(view)})
  unless(viewObj error("OA view deletion target does not exist"))
  deleted = ddDeleteObj(viewObj)
  unless(deleted error("OA view delete failed"))
  when(ddGetObj({skill_quote(library)} {skill_quote(cell)} {skill_quote(view)})
    error("OA view remained registered after delete"))
  unless(ddGetObj({skill_quote(library)} {skill_quote(cell)})
    error("OA parent cell disappeared during view delete"))
  t
)'''
    label = f"delete OA view {library}/{cell}/{view}"
    result = require_bridge_confirmation(
        operation,
        label,
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=cell,
            phase=label,
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    own_synchronous_cellview_delta_skill(source, label=label),
                    label=label,
                    mutation_target=(library, (cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to delete OA view {library}/{cell}/{view}: {result.errors[0]}"
        )
    if decode_skill_output(result.output or "").strip() != "t":
        raise RuntimeError(
            f"unexpected OA view deletion result for "
            f"{library}/{cell}/{view}: {result.output!r}"
        )


def assert_cell_has_no_open_windows(client: Any, library: str, cell: str) -> None:
    """Refuse generated-view replacement while any target cell window is open."""

    key = f"{library} {cell} "
    matches = [
        str(window.get("name", ""))
        for window in client.list_windows()
        if key in str(window.get("name", ""))
    ]
    if matches:
        raise RuntimeError(
            f"{library}/{cell} has open Virtuoso windows; close them before OA rebuild: "
            + "; ".join(matches)
        )


def assert_cell_has_no_open_views(client: Any, library: str, cell: str) -> None:
    """Refuse hidden DB handles as well as visible editor windows."""

    result = client.execute_skill(
        f'''setof(cv dbGetOpenCellViews()
  cv~>libName == {skill_quote(library)} && cv~>cellName == {skill_quote(cell)})''',
        timeout=20,
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    if (result.output or "").strip() not in {"", "nil", '"nil"'}:
        raise RuntimeError(
            f"{library}/{cell} has hidden open database views; "
            "close the exact owning operation/handle before overwriting it; "
            "the workflow will not close unowned views"
        )


def cell_view_open_mode(client: Any, library: str, cell: str, view: str) -> str | None:
    result = client.execute_skill(
        f'''let((cv)
  cv = dbFindOpenCellViewByName({skill_quote(library)} {skill_quote(cell)} {skill_quote(view)})
  when(cv cv~>mode)
)''',
        timeout=20,
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    raw = (result.output or "").strip().strip('"')
    return None if raw in {"", "nil"} else raw


def open_cell_views(
    client: Any,
    *,
    library: str | None = None,
) -> tuple[OpenCellViewInfo, ...]:
    """Return exact open-view state without opening or closing any view."""

    library_filter = (
        "t"
        if library is None
        else f"equal(cv~>libName {skill_quote(library)})"
    )
    result = client.execute_skill(
        f'''let((out windows visible)
  out = ""
  windows = hiGetWindowList()
  foreach(cv dbGetOpenCellViews()
    when({library_filter}
      visible = nil
      foreach(window windows
        when(equal(geGetWindowCellView(window) cv) visible = t))
      out = strcat(out sprintf(nil "%s|%s|%s|%L|%L|%L\\n"
        cv~>libName cv~>cellName cv~>viewName cv~>mode visible cv))))
  out
)''',
        timeout=30,
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    decoded = decode_skill_output(result.output or "")
    views: list[OpenCellViewInfo] = []
    for line in decoded.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != 6:
            raise RuntimeError(f"invalid open-cellview inventory row: {line!r}")
        lib, cell, view, raw_mode, raw_visible, raw_identity = fields
        mode = raw_mode.strip().strip('"')
        visible = raw_visible.strip() == "t"
        identity = raw_identity.strip()
        if not lib or not cell or not view or not mode or not identity:
            raise RuntimeError(f"incomplete open-cellview inventory row: {line!r}")
        views.append(
            OpenCellViewInfo(
                library=lib,
                cell=cell,
                view=view,
                mode=mode,
                visible=visible,
                identity=identity,
            )
        )
    return tuple(sorted(views))


def close_exact_hidden_cell_views(
    client: Any,
    views: tuple[OpenCellViewInfo, ...],
    *,
    operation: Any,
    timeout: int = 60,
) -> None:
    """Close only explicitly identified hidden read-only cellview handles.

    This is a recovery/cleanup primitive for an operation that recorded exact
    dbId identities before losing its normal synchronous cleanup.  It refuses
    visible, writable, missing, or identity-replaced handles and never selects
    a cellview by library/cell/view name alone.
    """

    require_workspace_capability(
        operation,
        client,
        authority=WorkspaceAuthority.GUI,
    )
    if not views:
        raise ValueError("exact hidden-cellview cleanup requires at least one view")
    identities = tuple(view.identity for view in views)
    if any(not identity.startswith("db:") for identity in identities):
        raise ValueError("exact hidden-cellview cleanup requires dbId identities")
    if len(set(identities)) != len(identities):
        raise ValueError("exact hidden-cellview cleanup identities must be unique")
    if any(view.visible or view.mode != "r" for view in views):
        raise ValueError(
            "exact hidden-cellview cleanup accepts only hidden read-only views"
        )
    expected = " ".join(skill_quote(identity) for identity in identities)
    source = f'''let((flowExpected flowCurrent flowTargets flowCv flowWindow
  flowFailures flowCloseAttempt flowPurgeAttempt flowVisible)
  flowExpected = list({expected})
  flowCurrent = dbGetOpenCellViews()
  flowTargets = setof(flowCv flowCurrent
    member(sprintf(nil "%L" flowCv) flowExpected))
  unless(equal(length(flowTargets) length(flowExpected))
    error(sprintf(nil "exact hidden-cellview cleanup identity set changed: %L"
      flowExpected)))
  foreach(flowCv flowTargets
    unless(equal(flowCv~>mode "r")
      error(sprintf(nil "refusing writable handle cleanup: %L" flowCv)))
    flowVisible = nil
    foreach(flowWindow hiGetWindowList()
      when(equal(geGetWindowCellView(flowWindow) flowCv)
        flowVisible = t))
    when(flowVisible
      error(sprintf(nil "refusing visible handle cleanup: %L" flowCv)))
  )
  flowFailures = nil
  foreach(flowCv flowTargets
    flowCloseAttempt = errset(dbClose(flowCv) t)
    when(!flowCloseAttempt || member(flowCv dbGetOpenCellViews())
      flowPurgeAttempt = nil
      when(member(flowCv dbGetOpenCellViews())
        flowPurgeAttempt = errset(dbPurge(flowCv) t))
      when(!flowPurgeAttempt || member(flowCv dbGetOpenCellViews())
        flowFailures = cons(flowCv flowFailures))))
  when(flowFailures
    error(sprintf(nil "exact hidden-cellview cleanup failed: %L"
      reverse(flowFailures))))
  when(setof(flowCv dbGetOpenCellViews()
    member(sprintf(nil "%L" flowCv) flowExpected))
    error("exact hidden-cellview cleanup left a requested handle open"))
  t
)'''
    result = require_bridge_confirmation(
        operation,
        "close exact hidden read-only cellviews",
        lambda: client.execute_skill(source, timeout=timeout),
    )
    if result.errors:
        raise RuntimeError(
            "failed to close exact hidden read-only cellviews: "
            f"{result.errors[0]}"
        )
    if decode_skill_output(result.output or "").strip() != "t":
        raise RuntimeError(
            "unexpected exact hidden-cellview cleanup result: "
            f"{result.output!r}"
        )


def close_visible_cell_windows(
    client: Any,
    library: str,
    cell: str,
    view: str | None,
    *,
    operation: Any,
) -> WindowCloseResult:
    """Save and close only exact, visible target views; never guess by title."""

    require_workspace_capability(
        operation,
        client,
        authority=WorkspaceAuthority.GUI,
    )
    operation.require_project_library_target(client, library)
    view_match = "t" if view is None else f"cv~>viewName == {skill_quote(view)}"
    source = f'''let((targets openViews cv visible count remaining)
  targets = nil
  openViews = setof(item dbGetOpenCellViews()
    item~>libName == {skill_quote(library)} &&
    item~>cellName == {skill_quote(cell)} &&
    {('t' if view is None else f'item~>viewName == {skill_quote(view)}')})
  foreach(window hiGetWindowList()
    cv = geGetWindowCellView(window)
    when(cv && cv~>libName == {skill_quote(library)} &&
      cv~>cellName == {skill_quote(cell)} && {view_match}
      targets = cons(window targets)))
  foreach(item openViews
    visible = nil
    foreach(window targets
      when(equal(geGetWindowCellView(window) item) visible = t))
    unless(visible
      error("refusing to close: target has a hidden or unowned open cellview")))
  count = 0
  foreach(window targets
    cv = geGetWindowCellView(window)
    when(member(cv~>mode list("a" "w"))
      unless(dbSave(cv) error("target cellview save failed")))
    hiCloseWindow(window)
    count = count + 1)
  remaining = 0
  foreach(window hiGetWindowList()
    cv = geGetWindowCellView(window)
    when(cv && cv~>libName == {skill_quote(library)} &&
      cv~>cellName == {skill_quote(cell)} && {view_match}
      remaining = remaining + 1))
  when(remaining > 0
    error(sprintf(nil "target windows remained open: %d" remaining)))
  list(count remaining)
)'''
    result = require_bridge_confirmation(
        operation,
        f"close visible windows {library}/{cell}",
        lambda: client.execute_skill(source, timeout=60),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to close exact target {library}/{cell}"
            f"{('/' + view) if view else ''}: {result.errors[0]}"
        )
    raw = decode_skill_output(result.output or "").strip()
    match = re.fullmatch(r"\(?\s*(\d+)\s+(\d+)\s*\)?", raw)
    if match is None:
        raise RuntimeError(f"unexpected close-window result: {raw!r}")
    return WindowCloseResult(
        closed=int(match.group(1)),
        remaining=int(match.group(2)),
    )


def set_cell_port_directions(
    client: Any,
    library: str,
    cell: str,
    directions: Mapping[str, str],
    *,
    operation: Any,
    timeout: int = 120,
) -> None:
    """Validate exact terminals, set directions in schematic/symbol, and save."""

    expected = " ".join(skill_quote(name) for name in directions)
    clauses = "\n".join(
        f"        (equal(term~>name {skill_quote(name)}) {skill_quote(direction)})"
        for name, direction in directions.items()
    )
    source = f'''let((cv direction expected actual attempt)
  expected = list({expected})
  foreach(view list("schematic" "symbol")
    cv = nil
    attempt = errset(
      unwindProtect(
        progn(
          cv = dbOpenCellViewByType({skill_quote(library)} {skill_quote(cell)} view "" "a")
          unless(cv error(sprintf(nil "cannot open %s/%s/%s" {skill_quote(library)} {skill_quote(cell)} view)))
          actual = cv~>terminals~>name
          foreach(name expected unless(member(name actual) error(sprintf(nil "missing terminal %s in %s" name view))))
          foreach(name actual unless(member(name expected) error(sprintf(nil "unexpected terminal %s in %s" name view))))
          foreach(term cv~>terminals
            direction = cond(
{clauses}
              (t error(sprintf(nil "no direction for %s" term~>name)))
            )
            term~>direction = direction
          )
          when(equal(view "schematic") schCheck(cv))
          unless(dbSave(cv) error(sprintf(nil "save failed for %s" view)))
          t
        )
        when(cv unless(dbClose(cv) error(sprintf(nil "close failed for %s" view))) cv = nil)
      )
      nil
    )
    unless(attempt && car(attempt)
      error(sprintf(nil "port update failed for %s" view)))
  )
  t
)'''
    result = require_bridge_confirmation(
        operation,
        f"set port directions {library}/{cell}",
        lambda: dispatch_oa_mutation(
            operation,
            client,
            library=library,
            cell=cell,
            phase="set port directions SKILL dispatch",
            callback=lambda: client.execute_skill(
                audit_cellview_delta_skill(
                    own_synchronous_cellview_delta_skill(
                        source,
                        label=f"port update {library}/{cell}",
                    ),
                    label=f"port update {library}/{cell}",
                    mutation_target=(library, (cell,)),
                ),
                timeout=timeout,
            ),
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"failed to set {library}/{cell} port directions: {result.errors[0]}"
        )


def validate_cell_port_directions(
    client: Any,
    library: str,
    cell: str,
    directions: Mapping[str, str],
    *,
    operation: Any,
    timeout: int = 60,
) -> None:
    """Fail unless schematic and symbol expose exactly the declared directions."""

    for view in ("schematic", "symbol"):
        require_workspace_capability(
            operation,
            client,
            library=library,
            cell=cell,
            view=view,
        )
    owned_open = _owned_db_open_cellview_skill(
        library=library,
        cell=cell,
        view_expression="view",
        view_type="",
        mode="r",
        result_variable="cv",
        label=f"port validation {library}/{cell}",
    )
    source = f'''let((cv out)
  out = ""
  foreach(view list("schematic" "symbol")
    cv = nil
    unwindProtect(
      progn(
{owned_open}
        unless(cv error(sprintf(nil "cannot open %s/%s/%s" {skill_quote(library)} {skill_quote(cell)} view)))
        foreach(term cv~>terminals
          out = strcat(out sprintf(nil "T|%s|%s|%s\\n"
            view term~>name term~>direction)))
        t
      )
      when(cv unless(dbClose(cv) error(sprintf(nil "close failed for %s" view))) cv = nil)
    )
  )
  out
)'''
    result = require_bridge_confirmation(
        operation,
        f"validate port directions {library}/{cell}",
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    source,
                    label=f"port validation {library}/{cell}",
                ),
                label=f"port validation {library}/{cell}",
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"invalid {library}/{cell} port interface; run OA rebuild: {result.errors[0]}"
        )
    decoded = decode_skill_output(result.output or "")
    actual_directions: dict[str, dict[str, str]] = {
        "schematic": {},
        "symbol": {},
    }
    for raw_line in decoded.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split("|")
        if fields[0] == "T" and len(fields) == 4:
            view, name, direction = fields[1:]
            if view not in actual_directions:
                raise RuntimeError(f"unexpected OA view in port inventory: {view}")
            if name in actual_directions[view]:
                raise RuntimeError(f"duplicate OA terminal {library}/{cell}/{view}/{name}")
            actual_directions[view][name] = direction
        else:
            raise RuntimeError(f"invalid OA port inventory row: {raw_line!r}")
    for view in ("schematic", "symbol"):
        if actual_directions[view] != dict(directions):
            raise RuntimeError(
                f"direction inventory mismatch for {library}/{cell}/{view}: "
                f"got {actual_directions[view]} expected {dict(directions)}"
            )


def _engineering_decimal(value: str) -> Decimal | None:
    match = _ENGINEERING_LITERAL.fullmatch(value.strip())
    if match is None:
        return None
    try:
        number = Decimal(match.group("number"))
        suffix = (match.group("suffix") or "").lower()
        if suffix:
            number *= _ENGINEERING_SCALE[suffix]
    except (InvalidOperation, KeyError):
        return None
    return number


def _instance_parameter_value_matches(expected: str, actual: str) -> bool:
    """Compare source Spectre spelling with the generated OA property value."""

    if _PARAMETER_REFERENCE.fullmatch(expected):
        return actual.replace(" ", "") == f'pPar("{expected}")'
    expected_number = _engineering_decimal(expected)
    actual_number = _engineering_decimal(actual)
    if expected_number is not None or actual_number is not None:
        return expected_number is not None and expected_number == actual_number
    return expected == actual


def validate_instance_parameters(
    client: Any,
    library: str,
    cell: str,
    expectations: Mapping[str, tuple[str, Mapping[str, str]]],
    *,
    operation: Any,
    timeout: int = 60,
) -> dict[str, object]:
    """Validate the exact source instance/master set and child parameters."""

    if not expectations:
        return {"passed": True, "instances": 0, "parameters": 0}
    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view="schematic",
    )
    owned_open = _owned_db_open_cellview_skill(
        library=library,
        cell=cell,
        view_expression='"schematic"',
        view_type="",
        mode="r",
        result_variable="cv",
        label=f"instance parameter validation {library}/{cell}",
    )
    queries: list[str] = []
    for instance, (_master, parameters) in sorted(expectations.items()):
        if parameters:
            queries.extend(
                (
                    f'inst = dbFindAnyInstByName(cv {skill_quote(instance)})',
                    "unless(inst error(sprintf(nil \"missing source-owned instance %s\" "
                    f"{skill_quote(instance)})))",
                )
            )
        for parameter in sorted(parameters):
            queries.extend(
                (
                    f'prop = dbFindProp(inst {skill_quote(parameter)})',
                    "if(prop then propValue = prop~>value "
                    "else propValue = \"<missing>\")",
                    "unless(stringp(propValue) "
                    "propValue = sprintf(nil \"%L\" propValue))",
                    "out = strcat(out sprintf(nil \"P|%s|%s|%s\\n\" "
                    f"{skill_quote(instance)} {skill_quote(parameter)} propValue))",
                )
            )
    source = f'''let((cv out inst prop propValue)
  out = ""
  cv = nil
  unwindProtect(
    progn(
{owned_open}
      unless(cv error("cannot open generated schematic"))
      foreach(inst cv~>instances
        out = strcat(out sprintf(nil "I|%s|%s|%s\\n"
          inst~>name inst~>master~>libName inst~>master~>cellName)))
      {chr(10).join(queries)}
      out
    )
    when(cv unless(dbClose(cv) error("generated schematic close failed")) cv = nil)
  )
)'''
    result = require_bridge_confirmation(
        operation,
        f"validate instance parameters {library}/{cell}",
        lambda: client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    source,
                    label=f"instance parameter validation {library}/{cell}",
                ),
                label=f"instance parameter validation {library}/{cell}",
            ),
            timeout=timeout,
        ),
    )
    if result.errors:
        raise RuntimeError(
            f"invalid {library}/{cell} instance parameters; run OA rebuild: "
            f"{result.errors[0]}"
        )
    masters: dict[str, str] = {}
    actual_parameters: dict[str, dict[str, str]] = {}
    for raw_line in decode_skill_output(result.output or "").splitlines():
        fields = raw_line.strip().split("|")
        if not fields or not fields[0]:
            continue
        if fields[0] == "I" and len(fields) == 4:
            if (
                fields[2] == "basic"
                and fields[3] in _SCHEMATIC_PIN_MASTER_CELLS
            ):
                continue
            if fields[1] in masters:
                raise RuntimeError(f"duplicate OA instance row: {fields[1]}")
            masters[fields[1]] = fields[3]
        elif fields[0] == "P" and len(fields) == 4:
            values = actual_parameters.setdefault(fields[1], {})
            if fields[2] in values:
                raise RuntimeError(
                    f"duplicate OA instance parameter row: {fields[1]}/{fields[2]}"
                )
            values[fields[2]] = fields[3]
        else:
            raise RuntimeError(f"invalid OA instance parameter row: {raw_line!r}")
    errors: list[str] = []
    missing = sorted(set(expectations) - set(masters))
    extra = sorted(set(masters) - set(expectations))
    if missing:
        errors.append(f"missing instances {missing!r}")
    if extra:
        errors.append(f"unexpected instances {extra!r}")
    for instance, (expected_master, parameters) in sorted(expectations.items()):
        actual_master = masters.get(instance)
        if actual_master != expected_master:
            errors.append(
                f"{instance} master got {actual_master!r}, expected {expected_master!r}"
            )
        actual_values = actual_parameters.get(instance, {})
        for name, expected_value in sorted(parameters.items()):
            actual_value = actual_values.get(name, "<missing>")
            if not _instance_parameter_value_matches(expected_value, actual_value):
                errors.append(
                    f"{instance}.{name} got {actual_value!r}, "
                    f"expected source value {expected_value!r}"
                )
    if errors:
        raise RuntimeError(
            f"stale generated instance parameters in {library}/{cell}: "
            + "; ".join(errors[:8])
            + (f"; and {len(errors) - 8} more" if len(errors) > 8 else "")
        )
    return {
        "passed": True,
        "instances": len(expectations),
        "parameters": sum(len(parameters) for _master, parameters in expectations.values()),
    }


def virtuoso_workdir(client: Any) -> Path:
    result = client.execute_skill("getWorkingDir()", timeout=20)
    if result.errors:
        raise RuntimeError(f"getWorkingDir failed: {result.errors[0]}")
    value = (result.output or "").strip().strip('"')
    if not value:
        raise RuntimeError("Virtuoso returned an empty working directory")
    return Path(value).resolve()
