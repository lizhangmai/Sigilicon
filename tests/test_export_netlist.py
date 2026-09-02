from __future__ import annotations

from pathlib import Path

from sigilicon.virtuoso.netlisting import _scoped_netlisting_skill


def test_scoped_netlisting_skill_contains_native_output_and_restores_session_setting(
    tmp_path: Path,
) -> None:
    source = (
        "let((vbSimResult vbNetlistResult) "
        "vbSimResult = errset(simulator('spectre) nil) "
        "when(isCallable('ddsRefresh) errset(ddsRefresh() nil)) "
        "vbNetlistResult = errset(createNetlist(?recreateAll t ?display nil) nil) "
        "vbNetlistResult)"
    )

    project_dir = tmp_path / "artifact work" / "native-netlist"
    rendered = _scoped_netlisting_skill(
        source,
        project_dir=project_dir,
        results_dir=project_dir,
    )

    assert "unwindProtect" in rendered
    assert "pre-existing OCEAN session blocks isolated netlist export" in rendered
    assert "and(flowPreviousSimulator car(flowPreviousSimulator))" in rendered
    assert "ocnCloseSession()" in rendered
    assert "failed to close artifact-owned OCEAN netlist session" in rendered
    assert "and(flowSimulatorAfterClose car(flowSimulatorAfterClose))" in rendered
    results_dir = f'resultsDir("{project_dir}")'
    assert results_dir in rendered
    project_dir_set = (
        f'envSetVal("asimenv.startup" "projectDir" \'string "{project_dir}")'
    )
    assert project_dir_set in rendered
    assert "flowPreviousProjectDir = errset(envGetVal" in rendered
    assert "flowPreviousResultsDir = errset(resultsDir() nil)" in rendered
    assert "unless(flowPreviousResultsDir" in rendered
    assert "resultsDir(car(flowPreviousResultsDir)) nil" in rendered
    assert "flowRestoredResultsDir = errset(resultsDir() nil)" in rendered
    assert "equal(car(flowRestoredResultsDir) car(flowPreviousResultsDir))" in rendered
    assert "failed to restore OCEAN resultsDir after netlist export" in rendered
    assert "failed to restore OCEAN projectDir after netlist export" in rendered
    assert "ddsRefresh" in rendered
    assert rendered.index(project_dir_set) < rendered.index("vbSimResult = errset(simulator")
    assert rendered.index("ddsRefresh") < rendered.index(results_dir) < rendered.index("createNetlist")
