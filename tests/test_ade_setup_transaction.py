from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.ams.provenance import ams_fingerprint
from sigilicon.ams.render import render_testbench
from sigilicon.ams.artifacts import AdeSetupAttempt, load_committed_ade_setup
from sigilicon.artifacts import atomic_write_json
from sigilicon.ams.spec import load_ams_spec
from sigilicon.paths import ProjectContext
from sigilicon.workflows.ams_ade_run import run_ade
from sigilicon.workflows.ams_ade_setup import setup_ade


@contextmanager
def _workspace(client, root, name, **_kwargs):
    commits: list[SimpleNamespace] = []

    @contextmanager
    def lease(*_args, **_kwargs):
        yield SimpleNamespace(checkpoint=lambda _phase: None)

    @contextmanager
    def mutation_scope(*_args, **_kwargs):
        yield SimpleNamespace(quarantined=[])

    def defer_commit(callback, *, on_failure=None):
        commit = SimpleNamespace(
            callback=callback,
            on_failure=on_failure,
            result=None,
            completed=False,
        )
        commits.append(commit)
        return commit

    operation = SimpleNamespace(
        client=client,
        root=root,
        name=name,
        view_lease=lease,
        mutation_scope=mutation_scope,
        defer_commit=defer_commit,
        register_artifact=lambda record: record.bind_operation("a" * 32),
    )
    try:
        yield operation
    except BaseException as exc:
        for commit in commits:
            if commit.on_failure is not None:
                commit.on_failure(exc)
        raise
    else:
        for commit in commits:
            commit.result = commit.callback()
            commit.completed = True


def _patch_setup(monkeypatch, spec, *, fail_maestro: bool = False) -> None:
    monkeypatch.setattr("sigilicon.workflows.ams_ade_setup.workspace_operation", _workspace)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.sanitize_virtuoso_license_env",
        lambda _client, **_kwargs: None,
    )
    existence = iter((True, False, False))
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.cell_exists",
        lambda *_args: next(existence),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.validate_cell_port_directions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.set_cell_port_directions",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.legacy_ade.cell_view_exists",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.read_oa_load_instances",
        lambda *_args, **_kwargs: (
            {
                "instance": "CLOAD0",
                "library": "analogLib",
                "cell": "cap",
                "value": spec.simulation.interface.load_cap,
                "nets": (spec.design.outputs[0], "0"),
            },
        ),
    )
    def import_wrapper(_client, **kwargs):
        for view_name in ("schematic", "symbol"):
            view = (
                spec.design.project_root
                / "virtuoso"
                / kwargs["library"]
                / spec.wrapper_cell
                / view_name
            )
            view.mkdir(parents=True)
            (view / "master.tag").write_text(f"{view_name}\n", encoding="utf-8")
        return (spec.wrapper_cell,)

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.import_hierarchy",
        import_wrapper,
    )

    def create_systemverilog(
        _client, current_spec, attempt, **_kwargs
    ) -> Path:
        source = attempt.path("inputs", "systemverilog", f"{current_spec.testbench}.sv")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            render_testbench(
                current_spec,
                dut_cell=current_spec.design.cell,
                include_supplies=True,
                dump_vcd=False,
            ),
            encoding="utf-8",
        )
        view = (
            current_spec.design.project_root
            / "virtuoso"
            / current_spec.design.library
            / current_spec.testbench
            / "systemVerilog"
        )
        view.mkdir(parents=True)
        (view / "master.tag").write_text("systemverilog\n", encoding="utf-8")
        return source

    def create_config(_client, **kwargs) -> None:
        view = (
            spec.design.project_root
            / "virtuoso"
            / kwargs["library"]
            / kwargs["testbench"]
            / "config"
        )
        view.mkdir(parents=True)
        (view / "expand.cfg").write_text("config\n", encoding="utf-8")

    def create_maestro(_client, **kwargs) -> None:
        if fail_maestro:
            raise RuntimeError("maestro save failed")
        view = (
            spec.design.project_root
            / "virtuoso"
            / kwargs["library"]
            / kwargs["testbench"]
            / "maestro"
        )
        view.mkdir(parents=True)
        (view / "maestro.sdb").write_text("maestro\n", encoding="utf-8")

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup._create_systemverilog_view",
        create_systemverilog,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.create_config_view",
        create_config,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.create_maestro_view",
        create_maestro,
    )


def test_setup_commits_all_components_with_one_fingerprint(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_setup(monkeypatch, spec)

    result = setup_ade(spec, object(), artifact_root=tmp_path / "artifacts")
    namespace = ProjectContext.from_project_root(
        root,
        artifact_root=tmp_path / "artifacts",
    ).artifacts.ade(spec.design.library, spec.testbench)
    committed = load_committed_ade_setup(namespace, ams_fingerprint(spec))

    assert result.manifest_path == committed.manifest_path
    assert result.setup_dir == committed.setup_dir
    assert result.setup_dir.parent.parent.name == ams_fingerprint(spec)
    assert set(committed.components) == {
        "load",
        "systemverilog",
        "config",
        "maestro",
    }
    assert {
        component["setup_fingerprint"] for component in committed.components.values()
    } == {ams_fingerprint(spec)}
    load = committed.components["load"]
    device_map = result.setup_dir / load["device_map"]
    assert device_map.read_text(encoding="utf-8") == "devselect := capacitor cap\n"
    assert load["device_map_sha256"]
    assert load["oa_load_attestation"]["load_cap"] == "2f"
    assert load["oa_load_attestation"]["instances"][0]["output"] == "OUT"
    maestro = committed.components["maestro"]
    assert maestro["model_file"] == str(spec.design.pdk.simulation.default.file)
    assert maestro["model_file_sha256"]
    assert maestro["model_section"] == spec.design.pdk.simulation.default.single_section
    current = json.loads((result.namespace_dir / "current.json").read_text())
    assert current["status"] == "succeeded"


def test_committed_setup_rejects_tampered_device_map(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_setup(monkeypatch, spec)
    result = setup_ade(spec, object(), artifact_root=tmp_path / "artifacts")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    device_map = result.setup_dir / manifest["details"]["components"]["load"]["device_map"]
    device_map.write_text("devselect := capacitor wrong_device\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="device map does not match"):
        namespace = ProjectContext.from_project_root(
            root,
            artifact_root=tmp_path / "artifacts",
        ).artifacts.ade(spec.design.library, spec.testbench)
        load_committed_ade_setup(namespace, ams_fingerprint(spec))


def test_model_content_is_part_of_ams_fingerprint(
    project_factory,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    original = ams_fingerprint(spec)

    spec.design.pdk.simulation.default.file.write_text(
        "// changed model\n", encoding="utf-8"
    )

    assert ams_fingerprint(spec) != original


@pytest.mark.parametrize(
    ("target", "message"),
    (
        ("systemverilog", "SystemVerilog source does not match"),
        ("device-map", "load device map does not match"),
        ("maestro-oa", "no longer matches"),
        ("model", "PDK model no longer matches"),
    ),
)
def test_setup_revalidates_generated_state_immediately_before_commit(
    monkeypatch,
    project_factory,
    tmp_path,
    target,
    message,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec)

    @contextmanager
    def mutating_workspace(client, workspace_root, name, **_kwargs):
        commits: list[SimpleNamespace] = []

        @contextmanager
        def lease(*_args, **_lease_kwargs):
            yield SimpleNamespace(checkpoint=lambda _phase: None)

        @contextmanager
        def mutation_scope(*_args, **_mutation_kwargs):
            yield SimpleNamespace(quarantined=[])

        def defer_commit(callback, *, on_failure=None):
            commit = SimpleNamespace(callback=callback, on_failure=on_failure)
            commits.append(commit)
            return commit

        operation = SimpleNamespace(
            client=client,
            root=workspace_root,
            name=name,
            view_lease=lease,
            mutation_scope=mutation_scope,
            defer_commit=defer_commit,
            register_artifact=lambda record: record.bind_operation("a" * 32),
        )
        yield operation
        if target == "systemverilog":
            changed = next(artifact_root.rglob("*.sv"))
        elif target == "device-map":
            changed = next(artifact_root.rglob("spiceIn.devmap"))
        elif target == "maestro-oa":
            changed = (
                root
                / "virtuoso"
                / spec.design.library
                / spec.testbench
                / "maestro"
                / "maestro.sdb"
            )
        else:
            changed = spec.design.pdk.simulation.default.file
        changed.write_text("changed before commit\n", encoding="utf-8")
        for commit in commits:
            try:
                commit.callback()
            except BaseException as error:
                if commit.on_failure is not None:
                    commit.on_failure(error)
                raise

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.workspace_operation",
        mutating_workspace,
    )

    with pytest.raises(RuntimeError, match=message):
        setup_ade(spec, object(), artifact_root=artifact_root)

    namespace = artifact_root / "verification" / spec.design.library / spec.testbench / "ade"
    assert not (namespace / "current.json").exists()
    manifests = [json.loads(path.read_text()) for path in namespace.glob("setups/**/manifest.json")]
    assert manifests and manifests[-1]["status"] != "succeeded"


def test_setup_assigns_declared_wrapper_port_directions(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    _patch_setup(monkeypatch, spec)
    calls: list[tuple[str, str, dict[str, str], str]] = []

    def record(_client, library, cell, directions, *, fingerprint, operation):
        assert operation.client is _client
        calls.append((library, cell, directions, fingerprint))

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.set_cell_port_directions",
        record,
    )

    setup_ade(spec, object(), artifact_root=tmp_path / "artifacts")

    assert calls == [
        (
            spec.design.library,
            spec.wrapper_cell,
            {"IN": "input", "OUT": "output"},
            ams_fingerprint(spec),
        )
    ]


def test_atomic_current_publication_failure_never_creates_a_fake_pointer(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec)

    def fail_current(_path, _value):
        raise OSError("current replace failed")

    monkeypatch.setattr("sigilicon.ams.artifacts.atomic_write_json", fail_current)

    with pytest.raises(OSError, match="current replace failed"):
        setup_ade(spec, object(), artifact_root=artifact_root)

    namespace = artifact_root / "verification" / spec.design.library / spec.testbench / "ade"
    assert not (namespace / "current.json").exists()
    manifests = [json.loads(path.read_text()) for path in namespace.glob("setups/**/manifest.json")]
    assert len(manifests) == 1
    assert manifests[0]["status"] == "succeeded"
    assert manifests[0]["completion_evidence"]


def test_workspace_entry_failure_terminalizes_the_setup_attempt(
    monkeypatch,
    project_factory,
    tmp_path,
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec)

    @contextmanager
    def refusing_workspace(*_args, **_kwargs):
        raise RuntimeError("workspace preflight refused")
        yield

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.workspace_operation",
        refusing_workspace,
    )

    with pytest.raises(RuntimeError, match="workspace preflight refused"):
        setup_ade(spec, object(), artifact_root=artifact_root)

    manifest_path = next(artifact_root.rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["operation_id"] is None
    assert not (
        artifact_root
        / "verification"
        / spec.design.library
        / spec.testbench
        / "ade/current.json"
    ).exists()


def test_begin_and_failed_setup_never_replace_a_previous_success_pointer(
    project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    namespace = ProjectContext.from_project_root(
        root,
        artifact_root=tmp_path / "artifacts",
    ).artifacts.ade(spec.design.library, spec.testbench)
    previous = {
        "artifact_kind": "ade_setup",
        "status": "succeeded",
        "setup_fingerprint": "1" * 64,
        "attempt_id": "2" * 32,
        "manifest": "verification/designLib/tb_inv/ade/previous/manifest.json",
    }
    atomic_write_json(namespace.current, previous)

    attempt = AdeSetupAttempt.begin(
        namespace,
        "3" * 64,
        attempt_id="4" * 32,
        entities={
            "library": spec.design.library,
            "cell": spec.design.cell,
            "testbench": spec.testbench,
        },
        operation="setup-ams-ade",
        backend="offline",
        source_fingerprint="5" * 64,
    )
    assert json.loads(namespace.current.read_text()) == previous
    attempt.record_failure(RuntimeError("setup failed"))
    assert json.loads(namespace.current.read_text()) == previous


def test_partial_setup_remains_incomplete_and_cannot_run(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec, fail_maestro=True)

    with pytest.raises(RuntimeError, match="maestro save failed"):
        setup_ade(spec, object(), artifact_root=artifact_root)

    namespace = artifact_root / "verification" / spec.design.library / spec.testbench / "ade"
    assert not (namespace / "current.json").exists()
    manifests = [json.loads(path.read_text()) for path in namespace.glob("setups/**/manifest.json")]
    assert manifests and manifests[-1]["status"] in {"partial", "uncertain", "failed"}

    monkeypatch.setattr("sigilicon.workflows.ams_ade_run.workspace_operation", _workspace)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.validate_cell_fingerprint",
        lambda *_args, **_kwargs: None,
    )
    started: list[bool] = []
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.run_isolated_maestro",
        lambda *_args, **_kwargs: started.append(True),
    )
    with pytest.raises(RuntimeError, match="cannot read ADE current"):
        run_ade(spec, object(), artifact_root=artifact_root)
    assert started == []


def test_setup_does_not_commit_when_final_view_reconciliation_fails(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec)

    @contextmanager
    def failing_workspace(client, workspace_root, name, **_kwargs):
        commits: list[SimpleNamespace] = []

        @contextmanager
        def lease(*_args, **_lease_kwargs):
            yield SimpleNamespace(checkpoint=lambda _phase: None)
            operation.uncertain_reason = "final view reconciliation failed"
            raise RuntimeError("final view reconciliation failed")

        @contextmanager
        def mutation_scope(*_args, **_mutation_kwargs):
            yield SimpleNamespace(quarantined=[])

        def defer_commit(callback, *, on_failure=None):
            commit = SimpleNamespace(
                callback=callback,
                on_failure=on_failure,
                completed=False,
            )
            commits.append(commit)
            return commit

        operation = SimpleNamespace(
            client=client,
            root=workspace_root,
            name=name,
            view_lease=lease,
            mutation_scope=mutation_scope,
            defer_commit=defer_commit,
            register_artifact=lambda record: record.bind_operation("a" * 32),
            uncertain_reason=None,
        )
        try:
            yield operation
        except BaseException as exc:
            for commit in commits:
                if commit.on_failure is not None:
                    commit.on_failure(exc)
            raise

    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_setup.workspace_operation",
        failing_workspace,
    )

    with pytest.raises(RuntimeError, match="final view reconciliation failed"):
        setup_ade(spec, object(), artifact_root=artifact_root)

    namespace = artifact_root / "verification" / spec.design.library / spec.testbench / "ade"
    assert not (namespace / "current.json").exists()
    manifests = [json.loads(path.read_text()) for path in namespace.glob("setups/**/manifest.json")]
    assert manifests and manifests[-1]["status"] == "uncertain"
    assert manifests[-1]["partial_failure"]["completed_components"] == [
        "load",
        "systemverilog",
        "config",
        "maestro",
    ]


def test_run_rejects_setup_fingerprint_mismatch(
    monkeypatch, project_factory, tmp_path
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec)
    setup_ade(spec, object(), artifact_root=artifact_root)

    text = path.read_text(encoding="utf-8").replace('settle = "5ns"', 'settle = "6ns"')
    path.write_text(text, encoding="utf-8")
    changed = load_ams_spec(path, project_root=root)
    monkeypatch.setattr("sigilicon.workflows.ams_ade_run.workspace_operation", _workspace)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.validate_cell_fingerprint",
        lambda *_args, **_kwargs: None,
    )
    started: list[bool] = []
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.run_isolated_maestro",
        lambda *_args, **_kwargs: started.append(True),
    )

    with pytest.raises(RuntimeError, match="fingerprint does not match"):
        run_ade(changed, object(), artifact_root=artifact_root)
    assert started == []


@pytest.mark.parametrize(
    ("cell_kind", "view", "filename"),
    (
        ("wrapper", "schematic", "master.tag"),
        ("testbench", "systemVerilog", "master.tag"),
        ("testbench", "config", "expand.cfg"),
        ("testbench", "maestro", "maestro.sdb"),
    ),
)
def test_run_rejects_any_oa_component_changed_after_commit(
    monkeypatch, project_factory, tmp_path, cell_kind, view, filename
) -> None:
    root, path = project_factory()
    spec = load_ams_spec(path, project_root=root)
    artifact_root = tmp_path / "artifacts"
    _patch_setup(monkeypatch, spec)
    setup_ade(spec, object(), artifact_root=artifact_root)
    cell = spec.wrapper_cell if cell_kind == "wrapper" else spec.testbench
    changed = root / "virtuoso" / spec.design.library / cell / view / filename
    changed.write_text("modified after commit\n", encoding="utf-8")

    monkeypatch.setattr("sigilicon.workflows.ams_ade_run.workspace_operation", _workspace)
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.validate_cell_fingerprint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "sigilicon.virtuoso.legacy_ade.cell_view_exists",
        lambda *_args, **_kwargs: True,
    )
    started: list[bool] = []
    monkeypatch.setattr(
        "sigilicon.workflows.ams_ade_run.run_isolated_maestro",
        lambda *_args, **_kwargs: started.append(True),
    )

    with pytest.raises(RuntimeError, match="no longer matches"):
        run_ade(spec, object(), artifact_root=artifact_root)
    assert started == []
