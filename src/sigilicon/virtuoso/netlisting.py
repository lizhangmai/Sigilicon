"""Schematic netlist export adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sigilicon.virtuoso.bridge import escape_skill_string
from sigilicon.virtuoso.bridge import export_schematic_netlist

from sigilicon.virtuoso.capability import require_workspace_capability
from sigilicon.virtuoso.confirmation import require_bridge_confirmation
from sigilicon.virtuoso.oa import (
    audit_cellview_delta_skill,
    own_synchronous_cellview_delta_skill,
)


class _NetlistCleanupClient:
    """Scope implicit hierarchy views opened by one OCEAN netlist request."""

    def __init__(self, client: Any, *, library: str, cell: str) -> None:
        self._client = client
        self._label = f"netlist export {library}/{cell}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def execute_skill(self, source: str, **kwargs: Any) -> Any:
        return self._client.execute_skill(
            audit_cellview_delta_skill(
                own_synchronous_cellview_delta_skill(
                    source,
                    label=self._label,
                ),
                label=self._label,
            ),
            **kwargs,
        )


def _inject_artifact_directories_before_netlisting(
    source: str,
    *,
    project_dir: Path,
    results_dir: Path,
) -> str:
    """Redirect OCEAN's project and result directories into one artifact run.

    ``resultsDir`` controls PSF-style result files but not the location chosen
    by ``createNetlist``.  OCEAN reads ``asimenv.startup/projectDir`` when
    ``simulator`` starts its session, so that value must be installed before
    the bridge's simulator call.  The caller restores both values with
    ``unwindProtect``.
    """

    if not source.strip():
        raise ValueError("netlist export SKILL source must be non-empty")
    anchor = "vbNetlistResult = errset(createNetlist(?recreateAll t ?display nil) nil)"
    refresh_anchor = "when(isCallable('ddsRefresh) errset(ddsRefresh() nil))"
    simulator_anchor = "vbSimResult = errset(simulator("
    if (
        source.count(anchor) != 1
        or source.count(refresh_anchor) != 1
        or source.count(simulator_anchor) != 1
    ):
        raise RuntimeError(
            "virtuoso-bridge OCEAN netlisting shape changed; refusing to redirect artifact directories"
        )
    if (
        source.index(simulator_anchor) > source.index(refresh_anchor)
        or source.index(refresh_anchor) > source.index(anchor)
    ):
        raise RuntimeError("bridge OCEAN netlisting refresh no longer precedes createNetlist")
    escaped_project = escape_skill_string(str(project_dir))
    escaped_results = escape_skill_string(str(results_dir))
    source = source.replace(
        simulator_anchor,
        f'envSetVal("asimenv.startup" "projectDir" \'string "{escaped_project}") '
        f"{simulator_anchor}",
        1,
    )
    return source.replace(anchor, f'resultsDir("{escaped_results}") {anchor}', 1)


def _scoped_netlisting_skill(source: str, *, project_dir: Path, results_dir: Path) -> str:
    """Run an OCEAN export with all native output contained in artifact work.

    The bridge intentionally delegates netlisting to OCEAN.  This adapter
    gives that one synchronous request a run-owned ``projectDir`` and
    ``resultsDir`` while restoring the user's previous environment settings
    on both success and failure.
    """

    scoped_source = _inject_artifact_directories_before_netlisting(
        source,
        project_dir=project_dir,
        results_dir=results_dir,
    )
    return f'''let((flowPreviousSimulator flowPreviousProjectDir flowPreviousResultsDir flowRestoreProjectDir flowRestoreResultsDir flowRestoredResultsDir flowCloseSession flowSimulatorAfterClose flowScopedNetlistResult)
  unless(and(isCallable('envGetVal) isCallable('envSetVal) isCallable('ocnCloseSession))
    error("OCEAN environment API unavailable for artifact-scoped netlisting"))
  flowPreviousSimulator = errset(simulator() nil)
  when(and(flowPreviousSimulator car(flowPreviousSimulator))
    error("pre-existing OCEAN session blocks isolated netlist export"))
  flowPreviousProjectDir = errset(envGetVal("asimenv.startup" "projectDir") nil)
  unless(and(flowPreviousProjectDir stringp(car(flowPreviousProjectDir)))
    error("cannot read current OCEAN projectDir before netlist export"))
  flowPreviousResultsDir = errset(resultsDir() nil)
  unless(flowPreviousResultsDir
    error("cannot read current OCEAN resultsDir before netlist export"))
  unwindProtect(
    progn(
      flowScopedNetlistResult = {scoped_source}
      flowScopedNetlistResult
    )
    progn(
      flowRestoreResultsDir = errset(
        resultsDir(car(flowPreviousResultsDir)) nil)
      flowRestoreProjectDir = errset(
        envSetVal("asimenv.startup" "projectDir" 'string car(flowPreviousProjectDir)) nil)
      flowRestoredResultsDir = errset(resultsDir() nil)
      unless(and(flowRestoredResultsDir
        equal(car(flowRestoredResultsDir) car(flowPreviousResultsDir)))
        error("failed to restore OCEAN resultsDir after netlist export"))
      unless(and(flowRestoreProjectDir car(flowRestoreProjectDir))
        error("failed to restore OCEAN projectDir after netlist export"))
      flowCloseSession = errset(ocnCloseSession() nil)
      flowSimulatorAfterClose = errset(simulator() nil)
      when(and(flowSimulatorAfterClose car(flowSimulatorAfterClose))
        error("failed to close artifact-owned OCEAN netlist session")))
  )
  flowScopedNetlistResult
)'''


class _ArtifactScopedNetlistClient(_NetlistCleanupClient):
    """Scope bridge OCEAN netlisting without changing bridge upstream code."""

    def __init__(self, client: Any, *, library: str, cell: str, project_dir: Path) -> None:
        super().__init__(client, library=library, cell=cell)
        self._project_dir = project_dir

    def execute_skill(self, source: str, **kwargs: Any) -> Any:
        return super().execute_skill(
            _scoped_netlisting_skill(
                source,
                project_dir=self._project_dir,
                results_dir=self._project_dir,
            ),
            **kwargs,
        )


def export_netlist(
    client: Any,
    library: str,
    cell: str,
    output_dir: Path,
    *,
    view: str,
    simulator: str,
    timeout: int,
    operation: Any,
) -> Path:
    require_workspace_capability(
        operation,
        client,
        library=library,
        cell=cell,
        view=view,
    )
    native_project_dir = output_dir / "native-netlist"
    native_project_dir.mkdir(parents=True, exist_ok=True)
    result = require_bridge_confirmation(
        operation,
        f"export netlist {library}/{cell}/{view}",
        lambda: export_schematic_netlist(
            _ArtifactScopedNetlistClient(
                client,
                library=library,
                cell=cell,
                project_dir=native_project_dir,
            ),
            library,
            cell,
            output_dir,
            view=view,
            simulator=simulator,
            timeout=timeout,
        ),
    )
    input_file = Path(result["input_file"])
    if not input_file.is_file():
        raise RuntimeError(f"netlist export did not produce its input file: {input_file}")
    return input_file
