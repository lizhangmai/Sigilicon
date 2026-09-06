"""Own HDB references introduced by one synchronous Maestro operation."""

from sigilicon.virtuoso.oa import skill_quote


def own_synchronous_config_skill(source: str, *, library: str, cell: str) -> str:
    """Anchor an initially closed config and release only references born here.

    Maestro can retain an HDB reference after maeCloseSession. A later write
    then upgrades that cached config and leaves an edit lock behind. Establish
    zero pre-existing references before acquiring our anchor; an already-open
    config is refused, never drained. Synchronous SKILL dispatch and the
    caller's workspace/view lease exclude another operation from this scope.
    A failed close retains its exact handle in the process scope record and
    blocks subsequent attempts; uncertain references are never retried blindly.
    """

    lib, target = skill_quote(library), skill_quote(cell)
    return f'''let((flowConfigProbe flowOwnedConfig flowConfigResult flowConfigCloses)
  when(boundp('flowHdbOwnedScope) && flowHdbOwnedScope
    error(sprintf(nil "an HDB scope remains uncertain; preserved %L" flowHdbOwnedScope)))
  flowConfigProbe = hdbOpen({lib} {target} "config" "r" "CDBA")
  unless(flowConfigProbe error("cannot inspect target HDB config"))
  flowHdbOwnedScope = list({lib} {target} "config" "preflight" flowConfigProbe)
  unless(hdbClose(flowConfigProbe)
    error("HDB preflight close uncertain; preserved exact scope handle"))
  flowHdbOwnedScope = nil
  when(hdbIsOpenConfig(flowConfigProbe)
    error("target HDB config was already open; preserved existing references"))
  flowOwnedConfig = hdbOpen({lib} {target} "config" "r" "CDBA")
  unless(flowOwnedConfig error("cannot acquire exact HDB config anchor"))
  flowHdbOwnedScope = list({lib} {target} "config" "anchor" flowOwnedConfig)
  flowConfigResult = unwindProtect(
    progn(
{source}
    )
    progn(
      when(maeGetSessions()
        error("Maestro session remains active; preserved owned HDB references"))
      unless(hdbIsOpenConfig(flowOwnedConfig)
        error("owned HDB anchor disappeared; preserved uncertain state"))
      flowConfigCloses = 0
      while(hdbIsOpenConfig(flowOwnedConfig) && flowConfigCloses < 16
        unless(equal(hdbGetLibName(flowOwnedConfig) {lib}) &&
               equal(hdbGetCellName(flowOwnedConfig) {target}) &&
               equal(hdbGetViewName(flowOwnedConfig) "config")
          error("owned HDB config identity changed; preserved references"))
        unless(hdbClose(flowOwnedConfig)
          error("owned HDB reference close failed"))
        flowConfigCloses = flowConfigCloses + 1)
      when(hdbIsOpenConfig(flowOwnedConfig)
        error("owned HDB references did not close within the cleanup bound"))
      flowHdbOwnedScope = nil)
  )
  flowConfigResult
)'''
