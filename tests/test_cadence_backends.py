from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from sigilicon.backends.cadence import (
    LayoutBackend,
    NativeOaBackend,
    XceliumBackend,
    _copy_isolated_project,
)
from sigilicon.execution import Resources, Step, StepContext


def _file(path: Path, text: str = "fixture\n", *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


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
    run_root = tmp_path / "run"
    work = run_root / "work" / step.id
    output = run_root / "outputs" / step.id
    sources = run_root / "inputs/sources"
    for root in (work, output, sources):
        root.mkdir(parents=True, exist_ok=True)
    return StepContext(
        "1" * 64,
        step,
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


def test_xcelium_backend_requires_explicit_sources_and_completion_marker(
    tmp_path: Path,
) -> None:
    marker = "RTL_SUMMARY failures=0"
    executable = _fake_xrun(tmp_path, marker)
    step = Step(
        "rtl",
        "cadence.xcelium",
        {
            "hdl_sources": ("rtl/design.sv", "dv/testbench.sv"),
            "success_marker": marker,
            "timeout_seconds": 10,
        },
        sources=("rtl/design.sv", "dv/testbench.sv"),
    )
    environment = dict(os.environ)
    environment["SIGILICON_CADENCE_XRUN"] = str(executable)
    resources = Resources(frozenset({"tool.cadence-xcelium"}), environment)
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
    backend = XceliumBackend()

    assert all(check.status == "ready" for check in backend.preflight(step, resources))
    result = backend.run(context)

    assert result.status == "succeeded"
    assert len(result.artifacts) == 4
    assert result.facts == {"passed": True}
    assert not (context.work_root / "xcelium.d").exists()


def test_cadence_isolated_project_preserves_owner_and_project_scopes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "source-project"
    owner = project / "ip/example"
    workspace = project / "workspace"
    owner.mkdir(parents=True)
    workspace.mkdir()
    project_source = _file(project / "configs/platform/catalog.toml")
    owner_source = _file(owner / "configs/ip.toml")
    step = Step(
        "oa",
        "cadence.native-oa",
        {"owner": "example", "testbench": "tb", "timeout_seconds": 10},
        sources=("configs/ip.toml", "configs/platform/catalog.toml"),
    )
    context = _context(
        tmp_path,
        step,
        Resources(),
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={
            "configs/ip.toml": "owner",
            "configs/platform/catalog.toml": "project",
        },
    )
    _file(context.source_root / "configs/ip.toml", owner_source.read_text())
    _file(
        context.source_root / "configs/platform/catalog.toml",
        project_source.read_text(),
    )

    isolated, owner_path = _copy_isolated_project(context, "example")

    assert owner_path == "ip/example"
    assert (isolated / "ip/example/configs/ip.toml").is_file()
    assert (isolated / "configs/platform/catalog.toml").is_file()
    assert "[components.example]" in (isolated / "ip/catalog.toml").read_text()


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
    context = _context(
        tmp_path,
        step,
        Resources(frozenset({"tool.virtuoso-bridge", "license.cadence-oa"})),
        project_root=project,
        owner_root=owner,
        workspace_root=workspace,
        scopes={source: "owner" for source in step.sources},
        register_operation=registered.append,
    )
    for source in step.sources:
        _file(context.source_root / source)
    return context


def _patch_isolated_owner(monkeypatch, tmp_path: Path):
    isolated = tmp_path / "isolated"
    owner_root = isolated / "ip/example"
    owner_root.mkdir(parents=True)
    project = SimpleNamespace(
        owner=lambda _name: SimpleNamespace(root=owner_root),
    )
    monkeypatch.setattr(
        "sigilicon.backends.cadence._copy_isolated_project",
        lambda _context, _owner: (isolated, "ip/example"),
    )
    monkeypatch.setattr(
        "sigilicon.domain.repository.Project.from_project_root",
        lambda _root: project,
    )
    return project


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
    project = _patch_isolated_owner(monkeypatch, tmp_path)
    selected = SimpleNamespace(cell="tb_EXAMPLE")
    plan = SimpleNamespace(testbenches=(selected,))
    monkeypatch.setattr(
        "sigilicon.workflows.project_oa.ProjectOaWorkflow",
        lambda selected_project, owner: SimpleNamespace(
            plan=lambda: plan if selected_project is project and owner == "example" else None
        ),
    )
    monkeypatch.setattr("sigilicon.workflows.oa_library.oa_plan_source_paths", lambda _plan: ())
    monkeypatch.setattr("sigilicon.virtuoso.client.get_client", lambda: object())

    def execute(_plan, _selected, _client, *, artifacts, bind_operation, **_kwargs):
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

    result = NativeOaBackend().run(context)

    assert result.status == "succeeded"
    assert result.facts == {"passed": True, "evidence_status": "passed"}
    assert len(result.artifacts) == 1
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
    project = _patch_isolated_owner(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.plan_layout_spec",
        lambda _spec, *, project: SimpleNamespace(project=project),
    )
    monkeypatch.setattr("sigilicon.virtuoso.client.get_client", lambda: object())

    def generate(_planning, _client, *, artifacts, bind_operation, **_kwargs):
        operation = SimpleNamespace(operation_id=context.operation_id)
        bind_operation(operation)
        artifacts.write_json("outputs", ("completion.json",), {"instances": 3})
        return SimpleNamespace(instance_count=3)

    monkeypatch.setattr(
        "sigilicon.workflows.layout_generation.generate_layout",
        generate,
    )

    result = LayoutBackend().run(context)

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
    second = _oa_context(tmp_path / "uncertain", step, registered=[])
    result = LayoutBackend().run(second)
    assert result.status == "uncertain"
    assert result.facts["workspace_uncertainty"] == (
        "workspace cleanup could not be proven",
    )
