"""Synchronous Maestro setup ownership and read-only process inventory."""

from __future__ import annotations

import re

from sigilicon.virtuoso.config import own_synchronous_config_skill
from sigilicon.virtuoso.oa import own_synchronous_cellview_delta_skill, skill_quote


def build_owned_maestro_setup_transaction_skill(
    lib: str,
    cell: str,
    *,
    scope_token: str,
    body: str,
    canonical_test: str | None = None,
) -> str:
    """Run setup mutation inside one exact open/capture/close registry primitive."""

    if not re.fullmatch(r"[0-9a-f]{32}", scope_token):
        raise ValueError("Maestro setup scope token must be 32 lowercase hex chars")
    if not body.strip():
        raise ValueError("Maestro setup transaction body must not be empty")
    if canonical_test is not None and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$]*", canonical_test
    ):
        raise ValueError("canonical Maestro test name must be an identifier")
    test_cleanup = ""
    if canonical_test is not None:
        test_cleanup = f'''      flowExistingTests = maeGetSetup(?session session)
      foreach(flowExistingTest flowExistingTests
        unless(equal(flowExistingTest {skill_quote(canonical_test)})
          unless(maeDeleteTest(flowExistingTest ?session session)
            error(sprintf(nil
              "failed to replace stale Maestro test %L" flowExistingTest)))))
'''
    test_assertion = ""
    if canonical_test is not None:
        test_assertion = f'''      flowFinalTests = maeGetSetup(?session session)
      unless(and(equal(length(flowFinalTests) 1)
                 member({skill_quote(canonical_test)} flowFinalTests))
        error(sprintf(nil
          "native Maestro setup test identity mismatch: %L" flowFinalTests)))
'''
    transaction = f'''unwindProtect(
    progn(
      flowOpenAttempted = t
      flowOpenAttempt = errset(
        maeOpenSetup(
          {skill_quote(lib)} {skill_quote(cell)} "maestro"
          ?application "Explorer" ?mode "a")
        nil)
      when(flowOpenAttempt && car(flowOpenAttempt)
        session = car(flowOpenAttempt))
      flowAfterViews = dbGetOpenCellViews()
      flowOwnedViews = nil
      foreach(flowCv flowAfterViews
        unless(member(flowCv flowBeforeViews)
          flowOwnedViews = cons(flowCv flowOwnedViews)))
      flowOwnedViews = reverse(flowOwnedViews)
      flowOwnershipComplete = t
      unless(boundp('flowMaestroOwnedScopes)
        flowMaestroOwnedScopes = nil)
      flowRecord = list({skill_quote(scope_token)} session
        flowOwnedViews flowOwnershipComplete)
      flowMaestroOwnedScopes = cons(
        flowRecord flowMaestroOwnedScopes)
      unless(session error("maeOpenSetup failed after exact ownership capture"))
{test_cleanup}{body}
{test_assertion}
      t
    )
    progn(
      if(!flowOpenAttempted || !flowRecord
        then flowCleanupFailures = cons(
          "Maestro ownership capture incomplete; preserved process state"
          flowCleanupFailures)
        else
          if(!session
            then flowCleanupFailures = cons(
              "Maestro open failed; preserved exact ownership scope"
              flowCleanupFailures)
            else
              flowCurrentViews = dbGetOpenCellViews()
              flowWindows = setof(flowWindow hiGetWindowList()
                flowWindow != hiGetCIWindow() &&
                equal(hiGetWidgetType(flowWindow) "graphics"))
              foreach(flowOwnedCv flowOwnedViews
                unless(member(flowOwnedCv flowCurrentViews)
                  flowCleanupFailures = cons(
                    sprintf(nil "owned Maestro cellview disappeared %L"
                      flowOwnedCv)
                    flowCleanupFailures))
                foreach(flowOtherRecord flowMaestroOwnedScopes
                  when(!equal(flowOtherRecord flowRecord) &&
                    member(flowOwnedCv caddr(flowOtherRecord))
                    flowCleanupFailures = cons(
                      sprintf(nil "cellview belongs to multiple scopes %L"
                        flowOwnedCv)
                      flowCleanupFailures)))
                flowVisible = nil
                foreach(flowWindow flowWindows
                  when(equal(geGetWindowCellView(flowWindow) flowOwnedCv)
                    flowVisible = t))
                when(flowVisible
                  flowCleanupFailures = cons(
                    sprintf(nil "owned Maestro cellview became visible %L"
                      flowOwnedCv)
                    flowCleanupFailures)))
              unless(flowCleanupFailures
                flowCleanup = errset(
                  maeCloseSession(?session session ?forceClose nil) nil)
                if(flowCleanup && car(flowCleanup)
                  then
                    flowSessions = errset(maeGetSessions() nil)
                    when(!flowSessions || member(session car(flowSessions))
                      flowCleanupFailures = cons(
                        "owned Maestro session remained active"
                        flowCleanupFailures))
                  else flowCleanupFailures = cons(
                    "owned Maestro session close failed"
                    flowCleanupFailures)))
              unless(flowCleanupFailures flowComplete = t)))
      when(flowCleanupFailures
        error(sprintf(nil
          "Maestro setup cleanup incomplete/uncertain; preserved scope: %L"
          reverse(flowCleanupFailures))))
    )
  )'''
    owned_transaction = own_synchronous_cellview_delta_skill(
        own_synchronous_config_skill(transaction, library=lib, cell=cell),
        label=f"Maestro setup {lib}/{cell}",
    )
    return f'''let((flowBeforeViews flowAfterViews flowOwnedViews
  flowCurrentViews flowWindows flowVisible flowRecord
  flowCleanup flowCleanupFailures flowSessions flowComplete
  flowOpenAttempt flowOpenAttempted flowOwnershipComplete
  flowTargetConflicts flowExistingTests flowExistingTest flowFinalTests
  session toolSession sessionType ok)
  when(boundp('flowMaestroOwnedScopes) && flowMaestroOwnedScopes
    error("another owned Maestro view scope is still active"))
  flowBeforeViews = dbGetOpenCellViews()
  flowTargetConflicts = setof(flowCv flowBeforeViews
    equal(flowCv~>libName {skill_quote(lib)}) &&
    equal(flowCv~>cellName {skill_quote(cell)}))
  when(flowTargetConflicts
    error(sprintf(nil
      "Maestro setup target became busy before atomic dispatch: %L"
      flowTargetConflicts)))
  flowCleanupFailures = nil
  flowComplete = nil
  flowOpenAttempted = nil
  flowOwnershipComplete = nil
  flowRecord = nil
  session = nil
  {owned_transaction}
  flowCurrentViews = dbGetOpenCellViews()
  foreach(flowOwnedCv flowOwnedViews
    when(member(flowOwnedCv flowCurrentViews)
      error(sprintf(nil
        "exact owned identity still has unproven references %L" flowOwnedCv))))
  when(flowComplete
    flowMaestroOwnedScopes = remove(flowRecord flowMaestroOwnedScopes))
  t
)'''


def active_maestro_sessions(client, *, timeout: int = 20) -> tuple[str, ...]:
    """Return every active Maestro session without opening or closing GUI state."""

    result = client.execute_skill("maeGetSessions()", timeout=timeout)
    if result.errors:
        raise RuntimeError(result.errors[0])
    raw = (result.output or "").strip()
    if not raw or raw in {"nil", '"nil"'}:
        return ()
    quoted = tuple(re.findall(r'"([^"\\]+)"', raw))
    if quoted:
        return quoted
    tokens = tuple(
        token for token in re.split(r"[()\s]+", raw) if token and token != "nil"
    )
    return tokens


def assert_no_active_maestro_sessions(client, operation: str) -> None:
    sessions = active_maestro_sessions(client)
    if sessions:
        raise RuntimeError(
            f"refusing {operation} while Maestro sessions are active: "
            f"{', '.join(sessions)}"
        )
