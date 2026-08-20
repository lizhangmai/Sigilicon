from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from sigilicon.cli.export_netlist import main
from sigilicon.paths import ProjectContext
from sigilicon.workflows.virtuoso_operations import export_project_netlist
from sigilicon.virtuoso.netlisting import _scoped_netlisting_skill
from sigilicon.virtuoso.bridge import schematic_export_netlist_skill


def test_scoped_netlisting_skill_contains_native_output_and_restores_session_setting(
    tmp_path: Path,
) -> None:
    source = (
        "let((vbSimResult vbNetlistResult) "
        "vbSimResult = errset(simulator(\'spectre) nil) "
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
    assert "flowPreviousProjectDir = errset(envGetVal(\"asimenv.startup\" \"projectDir\") nil)" in rendered
    assert "flowPreviousResultsDir = errset(resultsDir() nil)" in rendered
    assert "unless(flowPreviousResultsDir" in rendered
    assert "resultsDir(car(flowPreviousResultsDir)) nil" in rendered
    assert "flowRestoredResultsDir = errset(resultsDir() nil)" in rendered
    assert "equal(car(flowRestoredResultsDir) car(flowPreviousResultsDir))" in rendered
    assert "envSetVal(\"asimenv.startup\" \"projectDir\" 'string car(flowPreviousProjectDir)) nil" in rendered
    assert "failed to restore OCEAN resultsDir after netlist export" in rendered
    assert "failed to restore OCEAN projectDir after netlist export" in rendered
    assert "ddsRefresh" in rendered
    assert rendered.index(project_dir_set) < rendered.index("vbSimResult = errset(simulator")
    assert rendered.index("ddsRefresh") < rendered.index(results_dir) < rendered.index("createNetlist")


def test_scoped_netlisting_skill_accepts_the_installed_bridge_shape(tmp_path: Path) -> None:
    bridge_source = schematic_export_netlist_skill("test_lib", "test_cell")

    rendered = _scoped_netlisting_skill(
        bridge_source,
        project_dir=tmp_path / "work",
        results_dir=tmp_path / "work",
    )

    assert rendered.count("createNetlist(?recreateAll t ?display nil)") == 1
    assert rendered.index('envSetVal("asimenv.startup" "projectDir" \'string') < rendered.index(
        "vbSimResult = errset(simulator"
    )
    assert rendered.index('resultsDir("') > rendered.index("ddsRefresh")


def test_export_defaults_to_immutable_artifact_namespace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output: list[Path | None] = []

    def export(_client, _paths, _lib, _cell, *, artifact_root=None, **_kwargs):
        output.append(artifact_root)
        input_file = tmp_path / "artifacts/input.scs"
        input_file.parent.mkdir(parents=True)
        input_file.write_text("simulator lang=spectre\n", encoding="utf-8")
        return SimpleNamespace(input_scs=input_file)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sigilicon.cli.export_netlist.export_project_netlist", export)

    assert main(["lib", "inv"], client_factory=object) == 0

    assert output == [None]


def test_export_workflow_owns_the_immutable_artifact_directory(
    monkeypatch, tmp_path: Path
) -> None:
    destinations: list[Path] = []

    @contextmanager
    def workspace(client, root, name, **_kwargs):
        deferred = []

        @contextmanager
        def lease(*_args, **_lease_kwargs):
            yield

        operation = SimpleNamespace(
            client=client,
            root=root,
            name=name,
            view_lease=lease,
            uncertain_reason=None,
            register_artifact=lambda record: record.bind_operation("a" * 32),
            defer_commit=lambda callback, *, on_failure=None: deferred.append(
                SimpleNamespace(callback=callback, on_failure=on_failure)
            ),
        )
        yield operation
        for commit in deferred:
            commit.callback()

    def export(_client, _library, _cell, destination, **_kwargs):
        destinations.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        result = destination / "input.scs"
        result.write_text("simulator lang=spectre\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.workspace_operation",
        workspace,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.export_netlist",
        export,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.new_identity",
        lambda: "1" * 32,
    )

    paths = ProjectContext.from_project_root(tmp_path)
    result = export_project_netlist(
        object(),
        paths,
        "lib",
        "inv",
        artifact_root=None,
        view="schematic",
        simulator="spectre",
        timeout=120,
    )

    expected = (
        tmp_path
        / "artifacts/designs/lib/inv/exports/netlist/schematic/spectre/runs"
        / ("1" * 32)
    )
    assert destinations == [expected / "work"]
    assert result.input_scs == expected / "results/input.scs"
    assert result.manifest_path == expected / "manifest.json"
    request = json.loads((expected / "inputs/oa-view.json").read_text())
    assert request == {
        "cell": "inv",
        "kind": "oa-view-reference",
        "library": "lib",
        "simulator": "spectre",
        "view": "schematic",
        "workspace": str(tmp_path / "virtuoso"),
    }


def test_export_workflow_copies_direct_relative_spectre_support_files(
    monkeypatch, tmp_path: Path
) -> None:
    @contextmanager
    def workspace(client, root, name, **_kwargs):
        deferred = []

        @contextmanager
        def lease(*_args, **_lease_kwargs):
            yield

        operation = SimpleNamespace(
            client=client,
            root=root,
            name=name,
            view_lease=lease,
            uncertain_reason=None,
            register_artifact=lambda record: record.bind_operation("b" * 32),
            defer_commit=lambda callback, *, on_failure=None: deferred.append(callback),
        )
        yield operation
        for callback in deferred:
            callback()

    def export(_client, _library, _cell, destination, **_kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "ade_e.scs").write_text("encrypted helper\n", encoding="utf-8")
        result = destination / "input.scs"
        result.write_text('include "ade_e.scs"\n', encoding="utf-8")
        return result

    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.workspace_operation", workspace
    )
    monkeypatch.setattr("sigilicon.workflows.virtuoso_operations.export_netlist", export)
    monkeypatch.setattr(
        "sigilicon.workflows.virtuoso_operations.new_identity", lambda: "2" * 32
    )

    result = export_project_netlist(
        object(),
        ProjectContext.from_project_root(tmp_path),
        "lib",
        "cell",
        view="schematic",
        simulator="spectre",
        timeout=120,
    )

    assert result.input_scs.read_text(encoding="utf-8") == 'include "ade_e.scs"\n'
    assert result.input_scs.with_name("ade_e.scs").read_text(encoding="utf-8") == "encrypted helper\n"
    assert result.support_files == (result.input_scs.with_name("ade_e.scs"),)
