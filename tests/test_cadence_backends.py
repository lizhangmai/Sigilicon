from __future__ import annotations

import inspect
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from sigilicon.backends.cadence import (
    LayoutAdapter,
    LayoutVerificationAdapter,
    NativeOaAdapter,
    XceliumAdapter,
    XceliumAmsAdapter,
    cadence_adapters,
)
from sigilicon.execution import (
    ContractError,
    Evidence,
    ExecutionError,
    Step,
    StepContext,
)
from sigilicon.execution.model import Resources
from sigilicon.workflows.oa_library import oa_plan_source_paths


def _file(path: Path, text: str = "fixture\n", *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


def test_oa_operations_have_fixed_backend_identities() -> None:
    names = {backend.name for backend in cadence_adapters()}

    assert "cadence.oa" not in names
    assert {
        "cadence.oa-check",
        "cadence.oa-rebuild",
        "cadence.oa-attest",
    }.issubset(names)


def test_oa_plan_closes_over_every_native_model_file(tmp_path: Path) -> None:
    model = _file(tmp_path / "model.scs")
    support = _file(tmp_path / "support.scs")
    setup = _file(tmp_path / "setup.il")
    testbench = _file(tmp_path / "testbench.scs")
    model_set = SimpleNamespace(files=(model, support))
    pdk = SimpleNamespace(
        source_paths=(),
        runtime_bound=True,
        simulation=SimpleNamespace(model_sets={"default": model_set}),
    )
    native_setup = SimpleNamespace(
        pdk=pdk,
        source_snapshot=SimpleNamespace(source_path=setup),
        rdb_contract=None,
    )
    simulation = SimpleNamespace(
        source_documents={},
        native_setup=native_setup,
    )
    plan = SimpleNamespace(
        source=SimpleNamespace(source_documents={}, cells=()),
        netlist_snapshots={},
        designs=(),
        layouts=(),
        testbenches=(
            SimpleNamespace(
                source_snapshot=SimpleNamespace(source_path=testbench),
                simulation=simulation,
            ),
        ),
        views=(),
    )

    closure = oa_plan_source_paths(plan)

    assert {model.resolve(), support.resolve()}.issubset(closure)

    native_setup.pdk = SimpleNamespace(
        source_paths=(),
        runtime_bound=False,
        simulation=SimpleNamespace(
            model_sets={
                "default": SimpleNamespace(
                    files=(PurePosixPath("model.scs"),)
                )
            }
        ),
    )
    source_closure = oa_plan_source_paths(plan)
    assert (Path.cwd() / "model.scs").resolve() not in source_closure


def test_cadence_run_methods_only_consume_prepared_domain_plans() -> None:
    backends = (
        XceliumAmsAdapter(),
        NativeOaAdapter(),
        LayoutAdapter(),
        LayoutVerificationAdapter(),
        *(backend for backend in cadence_adapters() if backend.name.startswith("cadence.oa-")),
    )

    for backend in backends:
        source = inspect.getsource(backend.run)
        assert "Project.open" not in source
        assert "plan_xcelium" not in source
        assert "plan_oa" not in source
        assert "plan_layout" not in source


def _context(
    tmp_path: Path,
    step: Step,
    resources: Resources,
    *,
    project_root: Path | None = None,
    owner_root: Path | None = None,
    workspace_root: Path | None = None,
    scopes: dict[str, str] | None = None,
    register_operation=None,
) -> StepContext:
    runtime_step = step
    run_root = tmp_path / "run"
    work = run_root / "work" / runtime_step.id
    output = run_root / "outputs" / runtime_step.id
    sources = run_root / "inputs/sources"
    for root in (work, output, sources):
        root.mkdir(parents=True, exist_ok=True)
    return StepContext(
        "1" * 64,
        runtime_step,
        "2" * 32,
        "3" * 64,
        work,
        output,
        sources,
        resources,
        {},
        project_root=project_root,
        owner_root=owner_root,
        workspace_root=workspace_root,
        source_scopes={} if scopes is None else scopes,
        _register_operation=register_operation,
    )


def _bind_plan(context: StepContext, step: Step) -> StepContext:
    root = context.source_root.parent / "resources"
    sealed = tuple(
        resource
        for resource in step._resource_bindings
        if resource.kind in {"file", "directory"}
    )
    if sealed:
        root.mkdir(exist_ok=True)
    for resource in step._resource_bindings:
        if resource.kind == "file":
            (root / resource.materialization_key).write_bytes(resource.data)
        elif resource.kind == "directory":
            target = root / resource.materialization_key
            target.mkdir()
            for directory in resource.directories:
                (target / directory).mkdir(parents=True)
            for item in resource.files:
                path = target / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item.data)
    return replace(
        context,
        step=step,
        source_scopes={source: "owner" for source in step.sources},
        resource_root=root if sealed else None,
        resource_digests={
            resource.identity: resource.sha256
            for resource in step._resource_bindings
        },
        resource_kinds={
            resource.identity: resource.kind
            for resource in step._resource_bindings
        },
    )


def _fake_xrun(tmp_path: Path, marker: str) -> Path:
    return _file(
        tmp_path / "site/xcelium/tools/bin/xrun",
        f"""#!/bin/sh
set -eu
log=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-log' ]; then
    shift
    log="$1"
  fi
  shift
done
printf '%s\\n' {marker!r}
printf 'native complete\\n' >"$log"
""",
        executable=True,
    )


def test_cadence_executable_does_not_fall_back_to_ambient_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = _file(tmp_path / "ambient/xrun", executable=True)
    monkeypatch.setenv("PATH", str(executable.parent))
    resources = Resources(
        environment={"PATH": str(executable.parent)},
    )
    step = Step(
        "rtl",
        "cadence.xcelium",
        {"success_marker": "RTL_SUMMARY failures=0", "timeout_seconds": 10},
        sources=("rtl/design.sv",),
    )

    checks = XceliumAdapter().preflight(step, resources)

    assert any(
        check.subject == "cadence.xrun" and check.status == "blocked"
        for check in checks
    )


def test_xcelium_backend_requires_explicit_sources_and_completion_marker(
    tmp_path: Path,
) -> None:
    marker = "RTL_SUMMARY failures=0"
    executable = _fake_xrun(tmp_path, marker)
    step = Step(
        "rtl",
        "cadence.xcelium",
        {
                "success_marker": marker,
            "timeout_seconds": 10,
        },
        sources=("rtl/design.sv", "dv/testbench.sv"),
    )
    resources = Resources(
        tools={"cadence.xrun": str(executable)},
        environment=dict(os.environ),
    )
    project = tmp_path / "project"
    owner = project / "ip/example"
    workspace = project / "workspace"
    owner.mkdir(parents=True)
    workspace.mkdir()
    context = _context(
        tmp_path,
        step,
        resources,
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={source: "owner" for source in step.sources},
    )
    _file(context.source_root / "rtl/design.sv", "module design; endmodule\n")
    _file(context.source_root / "dv/testbench.sv", "module testbench; endmodule\n")
    backend = XceliumAdapter()

    assert all(check.status == "ready" for check in backend.preflight(step, resources))
    result = backend.run(context, step)

    assert result.status == "succeeded"
    assert len(result.artifacts) == 4
    assert result.facts == {"passed": True}
    assert not (context.work_root / "xcelium.d").exists()
    with pytest.raises(ExecutionError, match="disagrees"):
        backend.run(replace(context, step=step), replace(step, request={}),)


def test_xcelium_ams_backend_uses_locked_plan_and_resource_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _file(tmp_path / "bin/xrun", executable=True)
    step = Step(
        "ams",
        "cadence.xcelium-ams",
        {
            "owner": "example",
            "cell": "dv/tb_ams/cell.toml",
            "timeout_seconds": 10,
        },
        sources=("dv/tb_ams/cell.toml",),
        evidence=Evidence("diagnostic", "l2", "native-adapter-wiring"),
    )
    resources = Resources(
        tools={"cadence.xrun": str(executable)},
        environment={"PATH": "/snapshot/bin"},
    )
    project = tmp_path / "source-project"
    owner = project / "ip/example"
    workspace = tmp_path / "workspace"
    owner.mkdir(parents=True)
    workspace.mkdir()
    contract = _file(owner / "dv/tb_ams/cell.toml")
    model = _file(tmp_path / "pdk/model.scs", "model snapshot\n")
    context = _context(
        tmp_path,
        step,
        resources,
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={"dv/tb_ams/cell.toml": "owner"},
    )
    _file(context.source_root / "dv/tb_ams/cell.toml")
    selected_project = SimpleNamespace(
        project_root=project,
        owner=lambda name: SimpleNamespace(root=owner) if name == "example" else None,
    )
    planning = SimpleNamespace(
        platform=SimpleNamespace(key="fixture-pdk"),
        spec=SimpleNamespace(
            cell="tb_ams",
            ams=SimpleNamespace(circuit_role="circuit_netlist"),
        ),
        source_records={
            contract: contract.read_text(encoding="utf-8"),
            model: model.read_text(encoding="utf-8"),
        },
        sources=(contract,),
        circuit_netlist=contract,
        model_set=SimpleNamespace(name="nominal", files=(model,)),
        integration_check={
            "dependency_releases": [
                {
                    "name": "native-provider",
                    "release_id": "development-fixture",
                }
            ]
        },
        resource_identities={
            model: "pdk:fixture-pdk:simulation/nominal/0-model.scs"
        },
        as_dict=lambda: {"cell": "tb_ams", "model": str(model)},
    )
    monkeypatch.setattr(
        "sigilicon.workflows.xcelium_ams.plan_xcelium_ams_cell",
        lambda _cell, *, project, resources: (
            planning if project is selected_project else None
        ),
    )

    def execute(
        _planning,
        *,
        artifacts,
        environment_values,
        source_paths,
        **_kwargs,
    ):
        assert environment_values is resources.environment
        assert source_paths[model] != model
        assert source_paths[model].read_text(encoding="utf-8") == "model snapshot\n"
        artifacts.write_json("outputs", ("summary.json",), {"passed": True})
        return SimpleNamespace(passed=True)

    monkeypatch.setattr(
        "sigilicon.workflows.xcelium_ams.execute_xcelium_ams_cell",
        execute,
    )
    backend = XceliumAmsAdapter()
    prepared = backend.plan(selected_project, step, resources)
    context = _bind_plan(context, prepared)
    monkeypatch.setattr("sigilicon.project.Project.open", lambda _root: pytest.fail("Cadence run reopened the Project"))

    record = prepared.record
    assert str(model) not in str(record)
    assert "model snapshot" not in str(record)

    assert all(
        check.status == "ready" for check in backend.preflight(prepared, resources)
    )
    result = backend.run(context, prepared)

    assert result.status == "succeeded"
    assert result.facts == {
        "passed": True,
        "evidence_role": "diagnostic",
        "evidence_level": "l2",
        "evidence_scope": "native-adapter-wiring",
        "product_qualification_conclusion": False,
    }


def _oa_context(
    tmp_path: Path,
    step: Step,
    *,
    registered: list[object],
) -> StepContext:
    project = tmp_path / "source-project"
    owner = project / "ip/example"
    workspace = tmp_path / "oa-workspace"
    owner.mkdir(parents=True)
    workspace.mkdir()
    runtime_step = step
    virtuoso = _file(tmp_path / "site/virtuoso", executable=True)
    context = _context(
        tmp_path,
        runtime_step,
        Resources(
            capabilities=frozenset(
                {"tool.virtuoso-bridge", "license.cadence-oa"}
            ),
            tools={"cadence.virtuoso": str(virtuoso)},
            values={
                "virtuoso-bridge.host": "127.0.0.1",
                "virtuoso-bridge.port": "65432",
            },
        ),
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={source: "owner" for source in runtime_step.sources},
        register_operation=registered.append,
    )
    for source in runtime_step.sources:
        _file(context.source_root / source)
        _file(owner / source)
    return context


def test_native_oa_preflight_requires_explicit_virtuoso_executable(
    tmp_path: Path,
) -> None:
    step = Step(
        "native",
        "cadence.native-oa",
        {"owner": "example", "testbench": "tb_EXAMPLE", "timeout_seconds": 10},
        sources=("configs/oa.toml",),
    )
    values = {
        "virtuoso-bridge.host": "127.0.0.1",
        "virtuoso-bridge.port": "65432",
    }
    capabilities = frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})

    blocked = NativeOaAdapter().preflight(
        step,
        Resources(capabilities=capabilities, values=values),
    )

    assert any(
        check.subject == "cadence.virtuoso"
        and check.status == "blocked"
        for check in blocked
    )

    executable = _file(tmp_path / "tools/virtuoso", executable=True)
    ready = NativeOaAdapter().preflight(
        step,
        Resources(
            capabilities=capabilities,
            tools={"cadence.virtuoso": str(executable)},
            values=values,
        ),
    )

    assert all(check.status == "ready" for check in ready)


def test_oa_rebuild_preflight_checks_its_prepared_subtools(tmp_path: Path) -> None:
    step = Step(
        "oa",
        "cadence.oa-rebuild",
        {
            "config": {"owner": "example", "timeout_seconds": 10},
            "prepared": {
                "runtime_executables": (
                    "cadence.spice-in",
                    "cadence.cds-text-to-5x",
                )
            },
        },
        sources=("configs/oa.toml",),
    )
    values = {
        "virtuoso-bridge.host": "127.0.0.1",
        "virtuoso-bridge.port": "65432",
    }
    capabilities = frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})
    backend = next(
        item for item in cadence_adapters() if item.name == "cadence.oa-rebuild"
    )

    blocked = backend.preflight(
        step,
        Resources(capabilities=capabilities, values=values),
    )

    assert {
        check.subject
        for check in blocked
        if check.status == "blocked"
    } == {"cadence.spice-in", "cadence.cds-text-to-5x"}

    spicein = _file(tmp_path / "tools/spiceIn", executable=True)
    text_import = _file(tmp_path / "tools/cdsTextTo5x", executable=True)
    ready = backend.preflight(
        step,
        Resources(
            capabilities=capabilities,
            tools={
                "cadence.spice-in": str(spicein),
                "cadence.cds-text-to-5x": str(text_import),
            },
            values=values,
        ),
    )

    assert all(check.status == "ready" for check in ready)


def test_native_oa_backend_binds_operation_and_publishes_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = Step(
        "native",
        "cadence.native-oa",
        {"owner": "example", "testbench": "tb_EXAMPLE", "timeout_seconds": 10},
        sources=("configs/oa.toml",),
    )
    registered: list[object] = []
    context = _oa_context(tmp_path, step, registered=registered)
    source_project = context.project_root
    assert source_project is not None
    owner_root = context.owner_root
    assert owner_root is not None
    selected = SimpleNamespace(cell="tb_EXAMPLE")
    plan = SimpleNamespace(
        library="example",
        testbenches=(selected,),
        as_dict=lambda: {"library": "example", "testbench": "tb_EXAMPLE"},
    )
    project = SimpleNamespace(
        project_root=source_project,
        owner=lambda name: SimpleNamespace(root=owner_root)
        if name == "example"
        else None,
        oa_assembly_for=lambda _root: owner_root / "configs/oa.toml",
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.load_platform_inventory",
        lambda _project, *, resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.plan_oa_library_rebuild",
        lambda _manifest, *, project, platform_inventory: plan,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.oa_plan_source_paths",
        lambda _plan: frozenset(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.validate_oa_plan_source_members",
        lambda _plan, _members: None,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_client.get_client",
        lambda _resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.build_oa_layout_ir",
        lambda plan, **_kwargs: plan,
    )

    def execute(_plan, _selected, _client, *, artifacts, bind_operation, **_kwargs):
        assert _kwargs["resources"] is context.resources
        operation = SimpleNamespace(operation_id=context.operation_id)
        bind_operation(operation)
        artifacts.write_json("outputs", ("evidence.json",), {"passed": True})
        return SimpleNamespace(
            passed=True,
            evidence=SimpleNamespace(status="passed"),
        )

    monkeypatch.setattr(
        "sigilicon.workflows.oa_simulation.execute_oa_maestro_testbench",
        execute,
    )
    backend = NativeOaAdapter()
    prepared = backend.plan(project, step, context.resources)
    context = _bind_plan(context, prepared)
    monkeypatch.setattr("sigilicon.project.Project.open", lambda _root: pytest.fail("Cadence run reopened the Project"))

    result = backend.run(context, prepared)

    assert result.status == "succeeded"
    assert result.facts == {"passed": True, "evidence_status": "passed"}
    assert len(result.artifacts) == 1
    assert registered[0].operation_id == context.operation_id


def test_oa_rebuild_backend_binds_every_mutation_to_the_execution(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = Step(
        "oa",
        "cadence.oa-rebuild",
        {"owner": "example", "timeout_seconds": 10},
        sources=("configs/oa.toml",),
    )
    registered: list[object] = []
    context = _oa_context(tmp_path, step, registered=registered)
    assert context.project_root is not None and context.owner_root is not None
    model = _file(tmp_path / "site/pdk/model.scs", "sealed model\n")
    manifest = context.owner_root / "configs/oa.toml"
    planning = SimpleNamespace(
        library="example",
        testbenches=(),
        source=SimpleNamespace(
            manifest_path=context.owner_root / "configs/oa.toml",
            project=object(),
        ),
        as_dict=lambda: {"library": "example"},
    )
    project = SimpleNamespace(
        project_root=context.project_root,
        owner=lambda _name: SimpleNamespace(root=context.owner_root),
        oa_assembly_for=lambda _root: context.owner_root / "configs/oa.toml",
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.load_platform_inventory",
        lambda _project, *, resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.plan_oa_library_rebuild",
        lambda _manifest, *, project, platform_inventory: planning,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.oa_plan_source_paths",
        lambda _plan: frozenset({manifest, model}),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.validate_oa_plan_source_members",
        lambda _plan, _members: None,
    )
    monkeypatch.setattr(
        "sigilicon.backends.cadence._oa_resource_identities",
        lambda _project, _plan, _required, _resources: {
            model: "pdk:fixture:simulation/nominal/model.scs"
        },
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_client.get_client",
        lambda _resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.build_oa_layout_ir",
        lambda plan, **_kwargs: plan,
    )

    def rebuild(
        _plan,
        _client,
        *,
        source_paths,
        resource_paths,
        operation_id,
        bind_operation,
        **_kwargs,
    ):
        assert _kwargs["resources"] is context.resources
        assert source_paths[manifest] == context.source_root / "configs/oa.toml"
        assert source_paths[manifest] != manifest
        assert resource_paths[model].read_text(encoding="utf-8") == "sealed model\n"
        assert resource_paths[model] != model
        operation = SimpleNamespace(operation_id=operation_id)
        bind_operation(operation)
        return {"passed": True}

    monkeypatch.setattr(
        "sigilicon.workflows.oa_library.rebuild_oa_library",
        rebuild,
    )
    backend = next(
        backend for backend in cadence_adapters()
        if backend.name == "cadence.oa-rebuild"
    )
    prepared = backend.plan(project, step, context.resources)
    context = _bind_plan(context, prepared)
    monkeypatch.setattr("sigilicon.project.Project.open", lambda _root: pytest.fail("Cadence run reopened the Project"))

    result = backend.run(context, prepared)

    assert result.status == "succeeded"
    assert registered[0].operation_id == context.operation_id


def test_layout_backend_binds_mutation_and_preserves_uncertainty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = Step(
        "layout",
        "cadence.layout",
        {
            "owner": "example",
            "spec": "design/CELL/layout.toml",
            "timeout_seconds": 10,
        },
        sources=("design/CELL/layout.toml",),
    )
    registered: list[object] = []
    context = _oa_context(tmp_path, step, registered=registered)
    assert context.project_root is not None and context.owner_root is not None
    project = SimpleNamespace(
        project_root=context.project_root,
        owner=lambda _name: SimpleNamespace(root=context.owner_root),
    )
    source = context.owner_root / "design/CELL/layout.toml"
    planning = SimpleNamespace(
        source_records={source: source.read_text(encoding="utf-8")},
        plan=None,
        spec=SimpleNamespace(
            library="example",
            cell="CELL",
            view="layout",
            generator="fixture",
            stage="routed",
            pdk=SimpleNamespace(),
        ),
    )
    generated = SimpleNamespace(
        spec=planning.spec,
        plan=SimpleNamespace(canonical_json=lambda: '{"schema":1}\n'),
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.load_platform_inventory",
        lambda _project, *, resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.plan_layout_spec",
        lambda _spec, *, project, platform: planning,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_client.get_client",
        lambda _resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.build_managed_layout_ir",
        lambda _planning, **_kwargs: generated,
    )

    def generate(_planning, _client, *, artifacts, bind_operation, **_kwargs):
        operation = SimpleNamespace(operation_id=context.operation_id)
        bind_operation(operation)
        artifacts.write_json("outputs", ("completion.json",), {"instances": 3})
        return SimpleNamespace(instance_count=3)

    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.generate_layout",
        generate,
    )

    backend = LayoutAdapter()
    prepared = backend.plan(project, step, context.resources)
    context = _bind_plan(context, prepared)
    monkeypatch.setattr("sigilicon.project.Project.open", lambda _root: pytest.fail("Cadence run reopened the Project"))
    result = backend.run(context, prepared)

    assert result.status == "succeeded"
    assert result.facts == {"instance_count": 3}
    assert len(result.artifacts) == 1
    assert registered[0].operation_id == context.operation_id

    def uncertain(_planning, _client, *, record_uncertainty, **_kwargs):
        record_uncertainty("workspace cleanup could not be proven")
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.generate_layout",
        uncertain,
    )
    second = _bind_plan(
        _oa_context(tmp_path / "uncertain", step, registered=[]),
        prepared,
    )
    result = backend.run(second, prepared)
    assert result.status == "uncertain"
    assert result.facts["workspace_uncertainty"] == (
        "workspace cleanup could not be proven",
    )


def test_layout_backend_rejects_typed_source_snapshot_drift(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = Step(
        "layout",
        "cadence.layout",
        {
            "owner": "example",
            "spec": "design/CELL/layout.toml",
            "timeout_seconds": 10,
        },
        sources=("design/CELL/layout.toml",),
    )
    context = _oa_context(tmp_path, step, registered=[])
    assert context.project_root is not None and context.owner_root is not None
    source = context.owner_root / "design/CELL/layout.toml"
    project = SimpleNamespace(
        project_root=context.project_root,
        owner=lambda _name: SimpleNamespace(root=context.owner_root),
    )
    planning = SimpleNamespace(
        source_records={source: "stale typed snapshot\n"},
        spec=SimpleNamespace(
            library="FIXTURE",
            cell="CELL",
            view="layout",
            generator="fixture",
            stage="source",
            pdk=object(),
        ),
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.load_platform_inventory",
        lambda _project, *, resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.plan_layout_spec",
        lambda _spec, *, project, platform: planning,
    )

    with pytest.raises(ContractError, match="typed adapter source snapshot drift"):
        LayoutAdapter().plan(project, step, context.resources)


def test_layout_verification_backend_publishes_classified_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    xstream = _file(tmp_path / "bin/strmout", executable=True)
    calibre = _file(tmp_path / "bin/calibre", executable=True)
    step = Step(
        "verify",
        "cadence.layout-verify",
        {
            "owner": "example",
            "spec": "design/CELL/layout.toml",
            "check": "lvs",
            "xstream_timeout_seconds": 10,
            "calibre_timeout_seconds": 20,
        },
        sources=("design/CELL/layout.toml",),
        evidence=Evidence("regression", "l1", "physical-layout"),
    )
    resources = Resources(
        capabilities=frozenset(
            {
                "tool.virtuoso-bridge",
                "license.cadence-oa",
            }
        ),
        tools={
            "cadence.xstream": str(xstream),
            "mentor.calibre": str(calibre),
        },
        values={
            "virtuoso-bridge.host": "127.0.0.1",
            "virtuoso-bridge.port": "65432",
        },
    )
    project_root = tmp_path / "source-project"
    owner_root = project_root / "ip/example"
    workspace_root = tmp_path / "oa-workspace"
    owner_root.mkdir(parents=True)
    workspace_root.mkdir()
    registered: list[object] = []
    context = _context(
        tmp_path,
        step,
        resources,
        project_root=project_root,
        owner_root=owner_root,
        workspace_root=workspace_root,
        scopes={"design/CELL/layout.toml": "owner"},
        register_operation=registered.append,
    )
    _file(context.source_root / "design/CELL/layout.toml")
    source = _file(owner_root / "design/CELL/layout.toml")
    project = SimpleNamespace(
        project_root=project_root,
        owner=lambda _name: SimpleNamespace(root=owner_root),
    )
    layermap = _file(project_root / "configs/platform/pdk/layermap", "map\n")
    drc_deck = _file(project_root / "configs/platform/pdk/drc.deck", "drc\n")
    lvs_deck = _file(project_root / "configs/platform/pdk/lvs.deck", "lvs\n")
    planning = SimpleNamespace(
        spec=SimpleNamespace(
            library="example",
            cell="CELL",
            view="layout",
            generator="fixture",
            stage="routed",
            pdk=SimpleNamespace(key="fixture-pdk"),
            layout_pdk=SimpleNamespace(
                layermap=layermap,
                drc_deck=drc_deck,
                lvs_deck=lvs_deck,
            ),
        ),
        source_records={source: source.read_text(encoding="utf-8")},
        plan=None,
    )
    generated = SimpleNamespace(
        spec=planning.spec,
        plan=SimpleNamespace(canonical_json=lambda: '{"schema":1}\n'),
    )
    monkeypatch.setattr(
        "sigilicon.domain.platform.load_platform_inventory",
        lambda _project, *, resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.plan_layout_spec",
        lambda _spec, *, project, platform: planning,
    )
    monkeypatch.setattr(
        "sigilicon.workflows.oa_client.get_client",
        lambda _resources: object(),
    )
    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.build_managed_layout_ir",
        lambda _planning, **_kwargs: generated,
    )

    def verify(
        _planning,
        _client,
        *,
        artifacts,
        bind_operation,
        external_sources,
        **_kwargs,
    ):
        assert external_sources == {
            layermap: "map\n",
            lvs_deck: "lvs\n",
        }
        operation = SimpleNamespace(operation_id=context.operation_id)
        bind_operation(operation)
        artifacts.write_json("outputs", ("typed-evidence.json",), {"passed": True})
        evidence = SimpleNamespace(
            status=SimpleNamespace(value="clean"),
            canonical_json=lambda: '{"status":"clean"}\n',
        )
        return SimpleNamespace(passed=True, evidence=evidence)

    monkeypatch.setattr(
        "sigilicon.workflows.layout_verification.run_layout_verification",
        verify,
    )
    backend = LayoutVerificationAdapter()
    prepared = backend.plan(project, step, resources)
    context = _bind_plan(context, prepared)
    monkeypatch.setattr("sigilicon.project.Project.open", lambda _root: pytest.fail("Cadence run reopened the Project"))

    assert all(
        check.status == "ready" for check in backend.preflight(prepared, resources)
    )
    result = backend.run(context, prepared)

    assert result.status == "succeeded"
    assert result.facts == {
        "passed": True,
        "check": "lvs",
        "status": "clean",
        "evidence_role": "regression",
        "evidence_level": "l1",
        "evidence_scope": "physical-layout",
        "product_qualification_conclusion": False,
    }
    flow_evidence = (
        context.output_root / "verification/flow-evidence.json"
    ).read_text(encoding="utf-8")
    assert '"physical_verification":{"status":"clean"}' in flow_evidence
    assert registered[0].operation_id == context.operation_id
