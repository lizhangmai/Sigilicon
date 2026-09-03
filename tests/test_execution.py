from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

import sigilicon.execution.engine as execution_engine

from sigilicon.execution.adapter import Adapter
from sigilicon.execution._model import (
    Artifact,
    ContractError,
    ExecutionError,
    ExecutionPlan,
    Step,
    PreflightCheck,
    RunResult,
    Source,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.execution._model import ResourceBinding, Resources
from sigilicon.canonical import canonical_digest
from sigilicon.execution.operations import parse_selector
from sigilicon.execution.runs import RunStoreError, RunStore
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
from sigilicon.artifacts import RunRecord
from sigilicon.paths import ArtifactLayout
from sigilicon.project import Project


_TEST_ADAPTERS: tuple[object, ...] = ()


def _plan(project: Project, selector: str):
    return project.plan(selector)


def _run_store_call(
    project: Project,
    selector: str,
) -> tuple[RunStore, str, str, str | None]:
    owner, operation, variant = parse_selector(selector)
    return RunStore(project.artifact_root), owner, operation, variant


def _run_root(project: Project, selector: str, run_id: str) -> Path:
    owner, operation, variant = parse_selector(selector)
    return ArtifactLayout(project.artifact_root).operation_run(
        owner=owner,
        operation=operation,
        variant=variant,
        run_id=run_id,
    ).root


def _read_run(project: Project, selector: str, run_id: str):
    store, owner, operation, variant = _run_store_call(project, selector)
    return store.read(
        owner=owner,
        operation=operation,
        variant=variant,
        run_id=run_id,
    )


def _clean_run(project: Project, selector: str, run_id: str) -> None:
    store, owner, operation, variant = _run_store_call(project, selector)
    store.clean(
        owner=owner,
        operation=operation,
        variant=variant,
        run_id=run_id,
    )


def test_execution_interface_has_one_vocabulary_and_run_store_seam() -> None:
    import sigilicon.execution as execution

    public = (
        "Step",
        "Adapter",
        "ExecutionPlan",
        "RunResult",
        "RunStore",
        "StepWorkspace",
    )
    assert execution.__all__ == list(public)
    for name in public:
        assert getattr(execution, name).__name__ == name
    for removed in (
        "Artifact",
        "AdapterRegistry",
        "ContractError",
        "Evidence",
        "ExecutionError",
        "OperationStep",
        "PreparedStep",
        "Backend",
        "Operation",
        "Preparation",
        "PreflightCheck",
        "PreflightResult",
        "RunFailure",
        "RunStoreError",
        "RuntimeEnvironment",
        "Source",
        "StepContext",
        "StepOutcome",
        "StepResult",
        "_RunStore",
        "Resources",
    ):
        assert not hasattr(execution, removed)
    assert "_read_run" not in Project.__dict__
    assert "_clean_run" not in Project.__dict__


def test_large_chain_plan_has_linear_topology_and_cached_identity(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.sv"
    source_path.write_text("module source; endmodule\n", encoding="utf-8")
    source = Source.capture(source_path, root=tmp_path, scope="project")
    steps = tuple(
        Step(
            f"step-{index}",
            "fake.copy",
            {},
            needs=() if index == 0 else (f"step-{index - 1}",),
            sources=(source.path,),
        )
        for index in range(2_000)
    )

    plan = ExecutionPlan(
        project_identity=canonical_digest("project"),
        owner="owner",
        operation="large-chain",
        variant=None,
        steps=steps,
        sources=(source,),
        resources=(),
    )

    assert len(plan.steps) == 2_000
    assert plan.steps[-1].id == "step-1999"
    assert plan.identity is plan.identity


def test_resources_require_typed_project_configuration(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "runtime"
    directory.mkdir()
    resources = Resources(
        values={"test.value": "bound"},
        directories={"test.root": str(directory)},
    )

    assert resources.require_value("test.value") == "bound"
    assert resources.require_directory("test.root") == directory.resolve()

    with pytest.raises(ContractError, match="runtime value is missing"):
        resources.require_value("test.missing")
    with pytest.raises(ContractError, match="absolute path"):
        Resources(directories={"test.root": "relative"})
    file_path = tmp_path / "runtime-file"
    file_path.write_text("file\n", encoding="utf-8")
    with pytest.raises(ContractError, match="runtime directory is missing"):
        Resources(directories={"test.root": str(file_path)}).require_directory(
            "test.root"
        )


def test_source_reference_is_binary_safe_and_does_not_retain_payload(
    tmp_path: Path,
) -> None:
    payload = b"\x00\xffbinary\x00source"
    source_path = tmp_path / "macro.gds"
    source_path.write_bytes(payload)

    source = Source.capture(source_path, root=tmp_path)

    assert source.size == len(payload)
    assert source.read_bytes() == payload
    assert not hasattr(source, "text")
    assert source.current()


def test_step_workspace_does_not_import_the_execution_model() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/sigilicon/execution/_workspace.py"
    ).read_text(encoding="utf-8")

    assert "sigilicon.execution._model" not in source


@pytest.fixture(autouse=True)
def _bind_test_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    global _TEST_ADAPTERS
    _TEST_ADAPTERS = ()
    monkeypatch.setattr(
        "sigilicon.backends.trusted_adapters",
        lambda: _TEST_ADAPTERS,
    )
    monkeypatch.setenv("LM_LICENSE_FILE", "test-license")


def _write_project(root: Path) -> Path:
    (root / "catalogs").mkdir(parents=True, exist_ok=True)
    (root / "configs/platform").mkdir(parents=True, exist_ok=True)
    owner = root / "ip/example"
    (owner / "configs").mkdir(parents=True)
    (root / "sigilicon.toml").write_text(
        """schema = 1
contract_kind = "sigilicon-project"
path_scope = "repository"
owner = "test"

[catalogs]
ip = "catalogs/ip.toml"
platform = "configs/platform/catalog.toml"

[paths]
project_root = "."
workspace_root = "workspace"
artifact_root = "artifacts"

[runtime]
capabilities = ["offline"]
inherit_environment = ["LM_LICENSE_FILE"]

[runtime.tools]
"test.tool" = "/bin/true"

[runtime.values]
"test.value" = "configured"
""",
        encoding="utf-8",
    )
    (root / "catalogs/ip.toml").write_text(
        """schema = 1
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "test"

[components.example]
contract = "ip/example/component.toml"
root = "ip/example"
""",
        encoding="utf-8",
    )
    (root / "configs/platform/catalog.toml").write_text(
        """schema = 1
contract_kind = "platform-catalog"
path_scope = "repository"
owner = "test"

[platforms]
""",
        encoding="utf-8",
    )
    (owner / "component.toml").write_text(
        """schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "example"
name = "example"
kind = "rtl-ip"
operation_catalog = "operations"

[sources]
operations = "ip/example/configs/operations.toml"
value = "ip/example/configs/value.txt"

[filesets]
operation_catalog = ["operations"]
value = ["value"]
""",
        encoding="utf-8",
    )
    (owner / "configs/value.txt").write_text("hello\n", encoding="utf-8")
    operations = owner / "configs/operations.toml"
    operations.write_text(
        """schema = 3
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"

[runtime_defaults]
"fake.copy" = "copy"

[runtime.copy]
values = { SELECTED_VALUE = "test.value" }

[operations.check]
uses = "fake.copy"
filesets = ["value"]
config = { text = "hello" }
evidence = { role = "regression", level = "l0", scope = "source" }

[operations."check@fast"]
uses = "fake.copy"
filesets = ["value"]
config = { text = "hello" }
evidence = { role = "regression", level = "l0", scope = "source" }

[operations.all]

[[operations.all.steps]]
id = "source"
uses = "fake.copy"
filesets = ["value"]
config = { text = "hello" }

[[operations.all.steps]]
id = "transform"
uses = "fake.upper"
needs = ["source"]
filesets = ["value"]
""",
        encoding="utf-8",
    )
    return operations


class CopyAdapter:
    name = "fake.copy"

    def plan(self, _project, step, _resources):
        return step

    def preflight(self, step, resources):
        return (
            PreflightCheck(
                "capability",
                "offline",
                "ready" if "offline" in resources.capabilities else "blocked",
            ),
        )

    def run(self, context: StepContext, step: Step) -> StepResult:
        output = context.write_text(
            "source", "value.txt", str(step.config["text"])
        )
        return StepResult.succeeded(
            artifacts=(Artifact("source", "text.plain", output),),
            facts={"length": len(str(step.config["text"]))},
        )


class UpperAdapter:
    name = "fake.upper"

    def plan(self, _project, step, _resources):
        return step

    def preflight(self, step, resources):
        return ()

    def run(self, context: StepContext, step: Step) -> StepResult:
        source = context.artifacts("source", "source")[0]
        output = context.write_text(
            "result",
            "value.txt",
            source.path.read_text(encoding="utf-8").upper(),
        )
        return StepResult.succeeded(
            artifacts=(Artifact("result", "text.plain", output),)
        )


def _project(root: Path, *adapters) -> Project:
    """Select test-only implementations through pytest's private assembly."""

    global _TEST_ADAPTERS
    _TEST_ADAPTERS = adapters
    return Project.open(root)


def test_large_chain_execution_does_not_rescan_the_global_source_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    steps = "\n".join(
        (
            "[[operations.scale.steps]]\n"
            f'id = "step-{index}"\n'
            'uses = "fake.noop"\n'
            'filesets = ["value"]\n'
            + (f'needs = ["step-{index - 1}"]\n' if index else "")
        )
        for index in range(250)
    )
    operations.write_text(
        operations.read_text(encoding="utf-8")
        + "\n[operations.scale]\n"
        + steps,
        encoding="utf-8",
    )

    class NoopAdapter:
        name = "fake.noop"

        def plan(self, _project, step, _resources):
            return step

        def preflight(self, _step, _resources):
            return ()

        def run(self, context: StepContext, step: Step) -> StepResult:
            context.require_step(step)
            return StepResult.succeeded()

    project = _project(tmp_path, NoopAdapter())
    plan = project.plan("example:scale")
    metadata_checks = 0
    original = Source.metadata_current

    def counted(source: Source) -> bool:
        nonlocal metadata_checks
        metadata_checks += 1
        return original(source)

    monkeypatch.setattr(Source, "metadata_current", counted)
    result = project.run(plan)

    assert result.status == "succeeded"
    assert len(result.outcomes) == 250
    assert metadata_checks <= len(plan.sources) + 2 * len(plan._composition_sources)


def test_project_plan_is_source_bound_and_preflight_has_no_side_effects(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(), UpperAdapter())
    plan = project.plan("example:check")

    assert plan.owner == "example"
    assert plan.operation == "check"
    assert plan.variant is None
    assert [step.uses for step in plan.steps] == ["fake.copy"]
    assert plan.steps[0].config == {"text": "hello"}
    assert plan.steps[0].record["prepared"] == {}
    assert plan.steps[0].evidence.record == {
        "role": "regression",
        "level": "l0",
        "scope": "source",
    }
    assert json.loads(json.dumps(plan.record))["schema"] == 13
    assert "resources_identity" not in plan.record
    assert [resource["identity"] for resource in plan.record["resources"]] == [
        "test.value"
    ]
    assert not hasattr(plan, "_adapters")
    assert not project.artifact_root.exists()
    checked = project.preflight(plan)
    assert checked.status == "ready"
    assert not project.artifact_root.exists()

    operations.write_text(operations.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match="another project composition"):
        project.preflight(plan)


def test_project_plan_identity_excludes_runtime_configuration(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    plan = project.plan("example:check")

    assert project.preflight(plan).status == "ready"
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'capabilities = ["offline"]', "capabilities = []"
        ),
        encoding="utf-8",
    )
    without_runtime = _project(tmp_path, CopyAdapter())
    current = without_runtime.plan("example:check")
    assert current.record == plan.record
    assert current.project_identity == plan.project_identity
    assert without_runtime.preflight(current).status == "blocked"
    with pytest.raises(ContractError, match="not produced by this Project"):
        without_runtime.preflight(plan)


def test_plan_identity_excludes_unselected_owner_changes(tmp_path: Path) -> None:
    _write_project(tmp_path)
    foreign = tmp_path / "ip/foreign"
    (foreign / "configs").mkdir(parents=True)
    (foreign / "component.toml").write_text(
        '''schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "foreign"
name = "foreign"
kind = "rtl-ip"
operation_catalog = "operations"

[sources]
operations = "ip/foreign/configs/operations.toml"

[filesets]
operation_catalog = ["operations"]
''',
        encoding="utf-8",
    )
    operations = foreign / "configs/operations.toml"
    operations.write_text(
        '''schema = 3
contract_kind = "owner-operations"
path_scope = "owner"
owner = "foreign"

[operations.check]
uses = "fake.copy"
filesets = ["operation_catalog"]
''',
        encoding="utf-8",
    )
    catalog = tmp_path / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + '''
[components.foreign]
contract = "ip/foreign/component.toml"
root = "ip/foreign"
''',
        encoding="utf-8",
    )

    before_project = _project(tmp_path, CopyAdapter())
    before = before_project.plan("example:check")
    repository_identity = before_project.identity
    operations.write_text(operations.read_text(encoding="utf-8") + "\n")
    after_project = _project(tmp_path, CopyAdapter())
    after = after_project.plan("example:check")

    assert after.record == before.record
    assert after_project.identity != repository_identity


def test_project_catalog_identity_matches_component_owner(tmp_path: Path) -> None:
    _write_project(tmp_path)
    component = tmp_path / "ip/example/component.toml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            'owner = "example"', 'owner = "different-owner"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="identity disagrees"):
        Project.open(tmp_path)


def test_project_runtime_configuration_replaces_sigilicon_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    monkeypatch.setenv("SIGILICON_TEST_TOOL", "/ambient/tool")
    monkeypatch.setenv("SIGILICON_TEST_VALUE", "ambient")
    monkeypatch.setenv("LM_LICENSE_FILE", "host-license")
    monkeypatch.setenv("UNDECLARED_SITE_VALUE", "must-not-leak")

    runtime = Project.open(tmp_path)._execution_resources()

    assert runtime.require_tool("test.tool") == Path("/bin/true")
    assert runtime.require_value("test.value") == "configured"
    assert "SIGILICON_TEST_TOOL" not in runtime.environment
    assert "SIGILICON_TEST_VALUE" not in runtime.environment
    assert runtime.environment["LM_LICENSE_FILE"] == "host-license"
    assert "UNDECLARED_SITE_VALUE" not in runtime.environment
    assert runtime.inherit_environment == ("LM_LICENSE_FILE",)

    plan = Project.open(tmp_path).plan("example:check")
    assert plan.steps[0].runtime.values == {"SELECTED_VALUE": "test.value"}


def test_project_rejects_runtime_manifest_drift(tmp_path: Path) -> None:
    _write_project(tmp_path)
    manifest = tmp_path / "sigilicon.toml"
    project = Project.open(tmp_path)
    original_identity = project.identity

    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("/bin/true", "/bin/false"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest snapshot source document drift"):
        project.resources()
    with pytest.raises(ValueError, match="manifest snapshot source document drift"):
        _ = project.identity
    assert original_identity


def test_engine_rejects_manifest_drift_after_project_plan_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import sigilicon.execution.engine as engine

    _write_project(tmp_path)
    manifest = tmp_path / "sigilicon.toml"
    project = _project(tmp_path, CopyAdapter())
    plan = project.plan("example:check")
    original_run = engine._run

    def drift_then_run(*args, **kwargs):
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(engine, "_run", drift_then_run)

    with pytest.raises(ExecutionError, match="operation preflight is blocked"):
        project.run(plan, run_id="f" * 32)


def test_project_freezes_inherited_environment_outside_its_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    monkeypatch.setenv("LM_LICENSE_FILE", "first-license")
    first = Project.open(tmp_path)
    first_identity = first.identity

    monkeypatch.setenv("LM_LICENSE_FILE", "second-license")
    second = Project.open(tmp_path)

    assert first.resources().environment == {"LM_LICENSE_FILE": "first-license"}
    assert second.resources().environment == {"LM_LICENSE_FILE": "second-license"}
    assert first.identity == first_identity
    assert second.identity == first_identity


def test_project_records_an_absent_declared_environment_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    monkeypatch.delenv("LM_LICENSE_FILE")
    absent = Project.open(tmp_path)

    assert absent.resources().environment == {}
    assert absent.resources().environment_record == {"LM_LICENSE_FILE": None}

    monkeypatch.setenv("LM_LICENSE_FILE", "now-present")
    assert Project.open(tmp_path).identity == absent.identity


def test_execution_resources_reject_a_tool_replaced_after_sealing(
    tmp_path: Path,
) -> None:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    resources = Resources(tools={"fixture.tool": str(tool)})
    planned = resources.capture("fixture.tool")
    execution = resources.for_execution((planned,), None)

    tool.unlink()
    tool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    tool.chmod(0o755)

    with pytest.raises(ExecutionError, match="changed after planning"):
        with execution.owned_tool("fixture.tool"):
            pytest.fail("a replaced planned tool must never be exposed")


def test_resource_binding_includes_its_configured_location(tmp_path: Path) -> None:
    first = tmp_path / "first-tool"
    second = tmp_path / "second-tool"
    first.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    first.chmod(0o755)
    os.link(first, second)

    planned = Resources(tools={"fixture.tool": str(first)}).capture("fixture.tool")
    relocated = Resources(tools={"fixture.tool": str(second)})

    assert not relocated.matches(planned)


def test_execution_plan_is_stable_across_project_processes(tmp_path: Path) -> None:
    _write_project(tmp_path)
    local = Project.open(tmp_path).plan("example:check")
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "from sigilicon.project import Project; "
                "print(json.dumps(Project.open(sys.argv[1]).plan("
                "sys.argv[2]).record, sort_keys=True))"
            ),
            str(tmp_path),
            "example:check",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == local.record


def test_project_plan_accepts_only_a_selector(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    record = project.plan("example:check").record

    with pytest.raises(TypeError, match="owner:operation selector"):
        project.plan(record)  # type: ignore[arg-type]


def test_cli_preflight_compiles_the_current_selector(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sigilicon.cli.main import main as sigilicon_main

    _write_project(tmp_path)
    _project(tmp_path, CopyAdapter())

    assert sigilicon_main(
        (
            "flow",
            "preflight",
            "example:check",
            "--project-root",
            str(tmp_path),
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_cli_run_compiles_the_current_selector(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sigilicon.cli.main import main as sigilicon_main

    _write_project(tmp_path)
    _project(tmp_path, CopyAdapter())

    assert sigilicon_main(
        (
            "flow",
            "run",
            "example:check",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "6" * 32,
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


def test_cli_audit_stream_verifies_a_closed_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sigilicon.cli.main import main as sigilicon_main

    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    result = project.run(project.plan("example:check"), run_id="8" * 32)

    assert sigilicon_main(
        (
            "flow",
            "audit",
            "example:check",
            result.run_id,
            "--project-root",
            str(tmp_path),
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def test_adapter_planning_closes_over_discovered_sources_deterministically(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    owner = tmp_path / "ip/example"
    first = owner / "configs/discovered-a.txt"
    second = owner / "configs/discovered-b.txt"
    first.write_text("a\n", encoding="utf-8")
    second.write_text("b\n", encoding="utf-8")

    class DiscoveringAdapter(CopyAdapter):
        def __init__(self, paths: tuple[Path, ...]) -> None:
            self.paths = paths

        def plan(self, project, step, resources):
            owner_root = project.owner("example").root
            sources = tuple(
                Source.capture(path, root=owner_root, scope="owner")
                for path in self.paths
            )
            names = tuple(sorted(source.path for source in sources))
            return replace(
                step,
                sources=tuple(dict.fromkeys((*step.sources, *names))),
                _source_snapshots=tuple(
                    sorted(
                        (*step._source_snapshots, *sources),
                        key=lambda source: source.path,
                    )
                ),
            )

    forward = _project(
        tmp_path,
        DiscoveringAdapter((first, second)),
    ).plan("example:check")
    reverse = _project(
        tmp_path,
        DiscoveringAdapter((second, first)),
    ).plan("example:check")

    assert forward.identity == reverse.identity
    assert forward.steps[0].sources == (
        "configs/value.txt",
        "configs/discovered-a.txt",
        "configs/discovered-b.txt",
    )
    verifier = _project(
        tmp_path,
        DiscoveringAdapter((first, second)),
    )
    with pytest.raises(ContractError, match="not produced by this Project"):
        verifier.preflight(forward)
    assert verifier.preflight(verifier.plan("example:check")).ready


def test_planning_rejects_a_compiled_source_change(tmp_path: Path) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class ChangingAdapter(CopyAdapter):
        def plan(self, project, step, resources):
            source.write_text("changed during planning\n", encoding="utf-8")
            return super().plan(project, step, resources)

    project = _project(tmp_path, ChangingAdapter())

    with pytest.raises(ContractError, match="source changed during planning"):
        _plan(project, "example:check")


def test_backend_cannot_discover_another_owners_source(tmp_path: Path) -> None:
    _write_project(tmp_path)
    catalog = tmp_path / "catalogs/ip.toml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + """
[components.foreign]
contract = "ip/foreign/component.toml"
root = "ip/foreign"
""",
        encoding="utf-8",
    )
    foreign = tmp_path / "ip/foreign"
    foreign.mkdir()
    (foreign / "component.toml").write_text(
        """schema = 3
contract_kind = "ip-component"
path_scope = "owner"
owner = "foreign"
name = "foreign"
kind = "rtl-ip"

[sources]
value = "ip/foreign/value.txt"

[filesets]
source = ["value"]
""",
        encoding="utf-8",
    )
    value = foreign / "value.txt"
    value.write_text("foreign\n", encoding="utf-8")

    class ForeignSourceAdapter(CopyAdapter):
        def plan(self, project, step, resources):
            source = Source.capture(value, root=foreign, scope="owner")
            return replace(
                step,
                sources=(*step.sources, source.path),
                _source_snapshots=(*step._source_snapshots, source),
            )

    project = _project(tmp_path, ForeignSourceAdapter())
    with pytest.raises(ContractError, match="owned outside 'example'"):
        _plan(project, "example:check")


def test_backend_cannot_discover_a_symlinked_source(tmp_path: Path) -> None:
    _write_project(tmp_path)
    owner = tmp_path / "ip/example/configs"
    target = owner / "real.txt"
    target.write_text("real\n", encoding="utf-8")
    link = owner / "linked.txt"
    link.symlink_to(target.name)

    class SymlinkSourceAdapter(CopyAdapter):
        def plan(self, project, step, resources):
            source = Source.capture(link, root=owner.parent, scope="owner")
            return replace(
                step,
                sources=(*step.sources, source.path),
                _source_snapshots=(*step._source_snapshots, source),
            )

    project = _project(tmp_path, SymlinkSourceAdapter())
    with pytest.raises(ContractError, match="non-symlink"):
        _plan(project, "example:check")


def test_operation_catalog_rejects_source_groups(tmp_path: Path) -> None:
    operations = _write_project(tmp_path)
    operations.write_text(
        operations.read_text(encoding="utf-8")
        + '\n[source_groups]\nlegacy = ["configs/value.txt"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="unknown owner operation fields"):
        _plan(Project.open(tmp_path), "example:check")


def test_operation_rejects_source_globs(tmp_path: Path) -> None:
    operations = _write_project(tmp_path)
    operations.write_text(
        operations.read_text(encoding="utf-8").replace(
            'filesets = ["value"]\nconfig = { text = "hello" }',
            'filesets = ["value"]\nsource_globs = ["rtl/**/*.sv"]\n'
            'config = { text = "hello" }',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="unknown fields.*source_globs"):
        _plan(_project(tmp_path, CopyAdapter()), "example:check")


def test_operation_rejects_legacy_target_selector(tmp_path: Path) -> None:
    _write_project(tmp_path)
    with pytest.raises(ContractError, match=r"owner:operation\[@variant\]"):
        _plan(Project.open(tmp_path), "example/smoke:check")


def test_component_rejects_unknown_field(tmp_path: Path) -> None:
    _write_project(tmp_path)
    component = tmp_path / "ip/example/component.toml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            'kind = "rtl-ip"', 'kind = "rtl-ip"\nunexpected = true'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown fields.*unexpected"):
        Project.open(tmp_path)


def test_project_runs_dag_and_run_store_validates_and_cleans_result(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(), UpperAdapter())
    plan = _plan(project, "example:all")
    progress: list[tuple[str, str]] = []

    result = project.run(
        plan,
        run_id="a" * 32,
        progress=lambda step, status: progress.append((step, status)),
    )

    assert result.status == "succeeded"
    assert [outcome.step for outcome in result.outcomes] == ["source", "transform"]
    assert result.outcomes[-1].result.artifacts[0].path.read_text(encoding="utf-8") == "HELLO"
    assert progress == [
        ("source", "running"),
        ("source", "succeeded"),
        ("transform", "running"),
        ("transform", "succeeded"),
    ]
    stored = _read_run(project, "example:all", "a" * 32)
    assert stored.status == "succeeded"
    assert stored.plan_identity == plan.identity

    _clean_run(project, "example:all", "a" * 32)
    assert not _run_root(project, "example:all", result.run_id).exists()
    with pytest.raises(RunStoreError):
        _read_run(project, "example:all", "a" * 32)


def test_run_store_can_clean_an_abandoned_running_run(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    run_id = "e" * 32
    paths = ArtifactLayout(project.artifact_root).operation_run(
        owner="example",
        operation="check",
        variant=None,
        run_id=run_id,
    )
    record = RunRecord.begin(
        paths,
        adapter="sigilicon.execution",
        source={"plan_identity": "f" * 64},
    )
    record.write_text("outputs", ("unregistered-after-crash.log",), "partial\n")

    _clean_run(project, "example:check", run_id)

    assert not paths.root.exists()


def test_run_clean_never_follows_a_role_replaced_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    result = project.run(
        _plan(project, "example:check"),
        run_id="7" * 32,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    original = RunStore._records

    def replace_after_validation(store, selected, manifest):
        records = original(store, selected, manifest)
        outputs = selected.paths.role("outputs")
        outputs.chmod(0o700)
        for child in outputs.rglob("*"):
            if child.is_file():
                child.chmod(0o600)
        for child in sorted(
            outputs.rglob("*"), key=lambda path: len(path.parts), reverse=True
        ):
            child.rmdir() if child.is_dir() else child.unlink()
        outputs.rmdir()
        outputs.symlink_to(outside, target_is_directory=True)
        return records

    monkeypatch.setattr(RunStore, "_records", replace_after_validation)

    _clean_run(project, "example:check", result.run_id)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not _run_root(project, "example:check", result.run_id).exists()


def test_variant_is_part_of_plan_run_and_artifact_identity(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    plan = _plan(project, "example:check@fast")
    result = project.run(
        plan,
        run_id="1" * 32,
    )

    assert plan.variant == "fast"
    assert result.variant == "fast"
    assert _run_root(project, "example:check@fast", result.run_id) == (
        tmp_path / "artifacts/runs/example/check/variants/fast" / ("1" * 32)
    )
    assert _read_run(project, "example:check@fast", result.run_id).variant == "fast"
    with pytest.raises(RunStoreError):
        _read_run(project, "example:check", result.run_id)


def test_missing_backend_blocks_preflight_and_run(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.open(tmp_path)
    plan = _plan(project, "example:check")
    checked = project.preflight(plan)
    assert checked.status == "blocked"
    missing = next(check for check in checked.checks if check.subject == "fake.copy")
    assert missing.status == "blocked"
    with pytest.raises(ExecutionError, match="preflight is blocked"):
        project.run(plan, run_id="2" * 32)


def test_adapter_defects_propagate_from_preflight(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class BrokenAdapter(CopyAdapter):
        def preflight(self, step, resources):
            raise TypeError("broken preflight implementation")

    project = _project(tmp_path, BrokenAdapter())

    with pytest.raises(TypeError, match="broken preflight implementation"):
        project.preflight(_plan(project, "example:check"))


def test_adapter_defects_propagate_after_terminalizing_the_run(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class BrokenAdapter(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            raise RuntimeError("broken run implementation")

    project = _project(tmp_path, BrokenAdapter())
    run_id = "0" * 32

    with pytest.raises(RuntimeError, match="broken run implementation"):
        project.run(_plan(project, "example:check"), run_id=run_id)

    failure = _read_run(project, "example:check", run_id)
    assert failure.record["contract_kind"] == "run-failure"
    assert failure.error_type == "RuntimeError"


def test_same_content_source_metadata_drift_keeps_the_sealed_snapshot(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class MutatingBackend(CopyAdapter):
        def preflight(self, step, resources):
            source.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return ()

    project = _project(tmp_path, MutatingBackend(),)
    plan = _plan(project, "example:check")

    result = project.run(plan, run_id="b" * 32)

    assert result.status == "succeeded"
    assert result.outcomes[0].result.artifacts[0].read_text() == "hello"


def test_backend_consumes_the_sealed_source_not_the_live_owner_file(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "ip/example/configs/value.txt"

    class SealedSourceBackend(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            live.write_text("later\n", encoding="utf-8")
            output = context.write_text(
                "source",
                "value.txt",
                context.source_text("configs/value.txt"),
            )
            return StepResult.succeeded(
                artifacts=(Artifact("source", "text.plain", output),)
            )

    project = _project(tmp_path, SealedSourceBackend(),)
    plan = _plan(project, "example:check")

    result = project.run(
        plan,
        run_id="f" * 32,
    )

    assert result.outcomes[0].result.artifacts[0].path.read_text() == "hello\n"
    sealed = _run_root(project, "example:check", result.run_id) / "inputs/sources/configs/value.txt"
    assert sealed.stat().st_mode & 0o777 == 0o444
    assert sealed.parent.stat().st_mode & 0o777 == 0o555


def test_sealed_input_mutation_is_uncertain_and_remains_readable(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)

    class MutatingAdapter(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            sealed = context.source_path("configs/value.txt")
            sealed.chmod(0o644)
            sealed.write_text("changed during execution\n", encoding="utf-8")
            raise RuntimeError("adapter failed after changing its input")

    project = _project(tmp_path, MutatingAdapter())
    run_id = "9" * 32

    with pytest.raises(ExecutionError, match="sealed adapter input changed"):
        project.run(_plan(project, "example:check"), run_id=run_id)

    failure = _read_run(project, "example:check", run_id)
    assert failure.status == "uncertain"
    assert failure.error_type == "InputIntegrityError"
    assert "sealed adapter input changed" in failure.message
    _clean_run(project, "example:check", run_id)


def test_external_resource_is_sealed_without_persisting_location_or_text(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "site/pdk/model.scs"
    live.parent.mkdir(parents=True)
    live.write_text("proprietary model\n", encoding="utf-8")

    class ResourceAdapter(CopyAdapter):
        def plan(self, _project, step, resources):
            resource = ResourceBinding.capture(
                live,
                identity="pdk:fixture:simulation/nominal/model.scs",
            )
            return replace(
                step,
                resources=(resource.identity,),
                _resource_bindings=(resource,),
            )

        def run(self, context: StepContext, step: Step) -> StepResult:
            live.write_text("changed after sealing\n", encoding="utf-8")
            output = context.write_text(
                "source",
                "model.scs",
                context.resource_text(step.resources[0]),
            )
            return StepResult.succeeded(
                artifacts=(Artifact("source", "text.model", output),)
            )

    project = _project(tmp_path, ResourceAdapter())
    plan = _plan(project, "example:check")

    assert str(live) not in str(plan.record)
    assert "proprietary model" not in str(plan.record)
    assert plan.record["resources"][0]["sha256"] == plan.resources[0].sha256

    result = project.run(
        plan,
        run_id="e" * 32,
    )

    output = result.outcomes[0].result.artifacts[0].path
    assert output.read_text(encoding="utf-8") == "proprietary model\n"
    persisted = (_run_root(project, "example:check", result.run_id) / "inputs/execution-plan.json").read_text(
        encoding="utf-8"
    )
    assert str(live) not in persisted
    assert "proprietary model" not in persisted


def test_binary_resource_is_sealed_without_text_decoding(tmp_path: Path) -> None:
    _write_project(tmp_path)
    live = tmp_path / "site/pdk/table.bin"
    live.parent.mkdir(parents=True)
    payload = b"\x00\xff\x10binary\x00"
    live.write_bytes(payload)

    class BinaryAdapter(CopyAdapter):
        def plan(self, _project, step, resources):
            resource = ResourceBinding.capture(live, identity="pdk:fixture/table")
            return replace(
                step,
                resources=(resource.identity,),
                _resource_bindings=(resource,),
            )

        def run(self, context: StepContext, step: Step) -> StepResult:
            assert context.resource_bytes(step.resources[0]) == payload
            with pytest.raises(ExecutionError, match="not UTF-8"):
                context.resource_text(step.resources[0])
            return StepResult.succeeded(facts={"size": len(payload)})

    project = _project(tmp_path, BinaryAdapter())
    result = project.run(
        _plan(project, "example:check"),
        run_id="b" * 32,
    )

    assert result.outcomes[0].result.facts == {"size": len(payload)}
    bindings = json.loads(
        (_run_root(project, "example:check", result.run_id) / "inputs/runtime-bindings.json").read_text()
    )
    assert bindings["configuration"]["tools"] == {}
    assert bindings["configuration"]["values"] == {
        "test.value": "configured"
    }
    assert set(bindings["environment"]) == {"LM_LICENSE_FILE"}
    assert bindings["environment"]["LM_LICENSE_FILE"].startswith("sha256-")
    assert "test-license" not in str(bindings)
    assert str(live) not in str(bindings)
    plan_resources = json.loads(
        (_run_root(project, "example:check", result.run_id) / "inputs/execution-plan.json").read_text()
    )["resources"]
    assert any(resource["kind"] == "file" for resource in plan_resources)
    store, owner, operation, variant = _run_store_call(project, "example:check")
    selected = store._select(
        owner=owner,
        operation=operation,
        variant=variant,
        run_id=result.run_id,
    )
    store._validate_runtime_bindings(selected.paths, bindings, plan_resources)
    drifted_resources = copy.deepcopy(plan_resources)
    next(
        resource for resource in drifted_resources if resource["kind"] == "file"
    )["size"] += 1
    with pytest.raises(RunStoreError, match="resource identity drift"):
        store._validate_runtime_bindings(
            selected.paths,
            bindings,
            drifted_resources,
        )


def test_directory_resource_is_sealed_as_a_deterministic_tree(tmp_path: Path) -> None:
    _write_project(tmp_path)
    live = tmp_path / "site/pdk/library"
    (live / "nested/empty").mkdir(parents=True)
    (live / "model.bin").write_bytes(b"\x00model")
    executable = live / "nested/tool"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)

    class DirectoryAdapter(CopyAdapter):
        def plan(self, _project, step, resources):
            resource = ResourceBinding.capture(live, identity="pdk:fixture/library")
            return replace(
                step,
                resources=(resource.identity,),
                _resource_bindings=(resource,),
            )

        def run(self, context: StepContext, step: Step) -> StepResult:
            sealed = context.resource_path(step.resources[0])
            assert (sealed / "model.bin").read_bytes() == b"\x00model"
            assert (sealed / "nested/empty").is_dir()
            assert os.access(sealed / "nested/tool", os.X_OK)
            return StepResult.succeeded(facts={"tree": True})

    project = _project(tmp_path, DirectoryAdapter())
    result = project.run(
        _plan(project, "example:check"),
        run_id="d" * 32,
    )

    assert result.outcomes[0].result.facts == {"tree": True}
    bindings = json.loads(
        (_run_root(project, "example:check", result.run_id) / "inputs/runtime-bindings.json").read_text()
    )
    assert "resources" not in bindings
    plan_resources = json.loads(
        (_run_root(project, "example:check", result.run_id) / "inputs/execution-plan.json").read_text()
    )["resources"]
    directory = next(
        resource for resource in plan_resources if resource["kind"] == "directory"
    )
    assert directory["directories"] == ["nested", "nested/empty"]


def test_directory_resource_rejects_symlink_members(tmp_path: Path) -> None:
    live = tmp_path / "library"
    live.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"target")
    (live / "link").symlink_to(target)

    with pytest.raises(ContractError, match="must not contain symlinks"):
        ResourceBinding.capture(live, identity="pdk:fixture/library")


def test_tool_resource_binds_a_multicall_symlink_and_its_exact_target(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    first.write_bytes(b"first tool\n")
    first.chmod(0o755)
    second = tmp_path / "second"
    second.write_bytes(b"second tool\n")
    second.chmod(0o755)
    launcher = tmp_path / "tool"
    launcher.symlink_to(first.name)
    resources = Resources(tools={"test.tool": str(launcher)})

    binding = resources.capture("test.tool")

    assert binding.location == launcher
    assert binding.read_bytes() == b"first tool\n"
    assert resources.matches(binding)

    launcher.unlink()
    launcher.symlink_to(second.name)
    assert not resources.matches(binding)


def test_run_store_keeps_tools_as_external_content_references(tmp_path: Path) -> None:
    _write_project(tmp_path)
    tool = tmp_path / "site/tool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("/bin/true", str(tool)),
        encoding="utf-8",
    )

    class ToolAdapter(CopyAdapter):
        def plan(self, _project, step, resources):
            binding = resources.capture("test.tool")
            return replace(
                step,
                resources=(binding.identity,),
                _resource_bindings=(binding,),
            )

    project = _project(tmp_path, ToolAdapter())
    result = project.run(_plan(project, "example:check"), run_id="6" * 32)

    assert not (_run_root(project, "example:check", result.run_id) / "inputs/resources").exists()
    tool.unlink()
    stored = _read_run(project, "example:check", result.run_id)
    assert stored.status == "succeeded"
    store, owner, operation, variant = _run_store_call(project, "example:check")
    store.audit(
        owner=owner,
        operation=operation,
        variant=variant,
        run_id=result.run_id,
    )


def test_external_resource_reader_rejects_sealed_content_tampering(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "site/pdk/model.scs"
    live.parent.mkdir(parents=True)
    live.write_text("trusted model\n", encoding="utf-8")

    class TamperingAdapter(CopyAdapter):
        def plan(self, _project, step, resources):
            resource = ResourceBinding.capture(
                live,
                identity="pdk:fixture:simulation/nominal/model.scs",
            )
            return replace(
                step,
                resources=(resource.identity,),
                _resource_bindings=(resource,),
            )

        def run(self, context: StepContext, step: Step) -> StepResult:
            sealed = context.resource_path(step.resources[0])
            metadata = sealed.stat()
            sealed.chmod(0o600)
            sealed.write_text("forged model!\n", encoding="utf-8")
            with pytest.raises(ExecutionError, match="resource identity drift"):
                context.resource_text(step.resources[0])
            sealed.write_text("trusted model\n", encoding="utf-8")
            os.utime(
                sealed,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
            )
            sealed.chmod(0o444)
            return StepResult.succeeded(facts={"tamper_rejected": True})

    project = _project(tmp_path, TamperingAdapter())
    plan = _plan(project, "example:check")

    run_id = "7" * 32
    with pytest.raises(ExecutionError, match="sealed adapter input changed"):
        project.run(plan, run_id=run_id)

    failure = _read_run(project, "example:check", run_id)
    assert failure.status == "uncertain"
    assert failure.error_type == "InputIntegrityError"


def test_project_rejects_a_plan_for_another_composition(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    plan = _plan(project, "example:check")
    forged = replace(plan, project_identity="sha256-" + "0" * 64)

    with pytest.raises(ContractError, match="another project composition"):
        project.preflight(forged)


def test_project_adapter_registry_cannot_be_injected_through_replace(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())

    with pytest.raises(ValueError, match="init=False"):
        replace(project, _adapter_registry=object())


def test_project_rejects_composition_source_drift(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    plan = _plan(project, "example:check")
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('owner = "test"', 'owner = "other"', 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="project manifest snapshot source document drift"):
        project.preflight(plan)


@pytest.mark.parametrize("relative", ("catalogs/ip.toml", "ip/example/component.toml"))
def test_run_rejects_composition_drift_after_project_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative: str,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    plan = _plan(project, "example:check")
    original = execution_engine._seal_sources

    def seal_then_change(record, current_plan):
        source_root = original(record, current_plan)
        path = tmp_path / relative
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return source_root

    monkeypatch.setattr(execution_engine, "_seal_sources", seal_then_change)

    with pytest.raises(ExecutionError, match="project composition changed"):
        project.run(plan, run_id="1" * 32)


def test_project_rejects_owner_python_registration_fields_without_importing(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    marker = tmp_path / "owner-module-executed"
    module = tmp_path / "ip/example/register.py"
    module.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[flow]\naction_modules = { example = "ip/example/register.py" }\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown fields.*flow"):
        Project.open(tmp_path)
    assert not marker.exists()


def test_public_execution_models_reject_inconsistent_values(tmp_path: Path) -> None:
    assert "run_root" not in RunResult.__dataclass_fields__
    with pytest.raises(ContractError, match="config must be a mapping"):
        Step("bad", "fake.copy", "not-config")  # type: ignore[arg-type]
    step = Step("good", "fake.copy", {"nested": {"value": [1, 2]}})
    with pytest.raises(TypeError):
        step.config["changed"] = True  # type: ignore[index]
    outcome = StepOutcome("run", "fake.copy", StepResult.succeeded())
    with pytest.raises(ContractError, match="disagrees"):
        RunResult(
            "example",
            "check",
            None,
            "7" * 32,
            "8" * 32,
            "9" * 64,
            "failed",
            (outcome,),
            tmp_path / "artifacts/run",
        )


def test_backend_cannot_publish_an_incomplete_output_inventory(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class ExtraOutputBackend(CopyAdapter):
        def preflight(self, step, resources):
            return ()

        def run(self, context: StepContext, step: Step) -> StepResult:
            published = context.write_text("source", "published.txt", "published")
            context.write_text("source", "extra.txt", "extra")
            return StepResult.succeeded(
                artifacts=(Artifact("source", "text.plain", published),)
            )

    project = _project(tmp_path, ExtraOutputBackend(),)
    plan = _plan(project, "example:check")

    with pytest.raises(ExecutionError, match="output inventory"):
        project.run(plan, run_id="c" * 32)


def test_failed_backend_cannot_leave_an_incomplete_output_inventory(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)

    class ExtraDiagnosticBackend(CopyAdapter):
        def preflight(self, step, resources):
            return ()

        def run(self, context: StepContext, step: Step) -> StepResult:
            context.write_text("diagnostic", "unpublished.log", "diagnostic\n")
            return StepResult.failed("tool failed")

    project = _project(tmp_path, ExtraDiagnosticBackend())
    plan = _plan(project, "example:check")

    with pytest.raises(ExecutionError, match="output inventory"):
        project.run(plan, run_id="d" * 32)


def test_uncertain_execution_is_distinct_from_closed_result_storage(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class UncertainBackend(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            return StepResult.uncertain("descendant cleanup could not be proven")

    project = _project(tmp_path, UncertainBackend(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="6" * 32,
    )

    assert result.status == "uncertain"
    assert json.loads((_run_root(project, "example:check", result.run_id) / "manifest.json").read_text())["status"] == (
        "uncertain"
    )
    restored = _read_run(project, "example:check", result.run_id)
    assert restored.status == "uncertain"


def test_process_cleanup_uncertainty_cannot_be_downgraded_to_failure(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)

    class CleanupUnknownBackend(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            raise ProcessGroupCleanupUncertainError(
                "descendant cleanup could not be proven"
            )

    project = _project(tmp_path, CleanupUnknownBackend(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="2" * 32,
    )

    assert result.status == "uncertain"
    assert result.outcomes[0].result.message == (
        "descendant cleanup could not be proven"
    )


def test_cancelled_execution_is_closed_and_restorable(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class CancelledBackend(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            return StepResult.cancelled("operator cancelled the tool")

    project = _project(tmp_path, CancelledBackend(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="3" * 32,
    )

    assert result.status == "cancelled"
    assert json.loads((_run_root(project, "example:check", result.run_id) / "manifest.json").read_text())["status"] == (
        "cancelled"
    )
    assert _read_run(project, "example:check", result.run_id).status == "cancelled"


def test_failed_step_keeps_its_diagnostic_evidence(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class RejectingBackend(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            evidence = context.write_text("evidence", "failure.json", "{}\n")
            return StepResult(
                "failed",
                (Artifact("evidence", "evidence.failure", evidence),),
                {"passed": False},
                "qualification failed",
            )

    project = _project(tmp_path, RejectingBackend(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="4" * 32,
    )

    assert result.status == "failed"
    assert json.loads((_run_root(project, "example:check", result.run_id) / "manifest.json").read_text())["status"] == (
        "failed"
    )
    assert result.outcomes[0].result.artifacts[0].path.read_text() == "{}\n"
    restored = _read_run(project, "example:check", result.run_id)
    assert restored.outcomes[0].result.facts == {"passed": False}
    artifact = restored.outcomes[0].result.artifacts[0]
    assert artifact.role == "evidence"
    assert artifact.read_text() == "{}\n"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    artifact.path.unlink()
    artifact.path.symlink_to(outside)
    with pytest.raises((OSError, RuntimeError)):
        artifact.read_text()
    assert restored.record["steps"][0]["artifacts"][0]["path"] == (
        "outputs/run/evidence/failure.json"
    )


def test_running_plan_is_independent_of_later_live_source_changes(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "ip/example/configs/value.txt"

    class DriftingCopyAdapter(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            result = super().run(context, step)
            live.write_text("changed\n", encoding="utf-8")
            return result

    project = _project(tmp_path, DriftingCopyAdapter(), UpperAdapter())
    plan = _plan(project, "example:all")

    result = project.run(plan, run_id="5" * 32)
    stored = _read_run(project, "example:all", "5" * 32)

    assert result.status == "succeeded"
    assert stored.status == "succeeded"
    assert stored.outcomes[-1].result.artifacts[0].read_text() == "HELLO"


def test_run_store_is_independent_of_current_operation_source_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="d" * 32,
    )
    operations.unlink()

    stored = _read_run(project, "example:check", result.run_id)
    assert stored.status == "succeeded"

    result_path = _run_root(project, "example:check", result.run_id) / "outputs/run-result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["variant"] = "tampered"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunStoreError, match="manifest|result"):
        _read_run(project, "example:check", result.run_id)


def test_run_store_rejects_unregistered_immutable_role_members(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    result = project.run(
        _plan(project, "example:check"),
        run_id="8" * 32,
    )
    (_run_root(project, "example:check", result.run_id) / "outputs/unregistered.txt").write_text(
        "not in manifest\n",
        encoding="utf-8",
    )

    with pytest.raises(RunStoreError, match="outputs inventory"):
        _read_run(project, "example:check", result.run_id)


def test_run_store_defers_artifact_hashing_until_payload_access_or_audit(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="9" * 32,
    )
    output = result.outcomes[0].result.artifacts[0].path
    output.write_text("jello", encoding="utf-8")

    stored = _read_run(project, "example:check", result.run_id)
    artifact = stored.outcomes[0].result.artifacts[0]
    with pytest.raises(ContractError, match="payload"):
        artifact.read_text()
    store, owner, operation, variant = _run_store_call(project, "example:check")
    with pytest.raises(RunStoreError, match="metadata"):
        store.audit(
            owner=owner,
            operation=operation,
            variant=variant,
            run_id=result.run_id,
        )


def test_run_identity_is_exclusive(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    plan = _plan(project, "example:check")
    project.run(plan, run_id="e" * 32)

    with pytest.raises(FileExistsError):
        project.run(plan, run_id="e" * 32)
    assert _read_run(project, "example:check", "e" * 32).status == "succeeded"


def test_concurrent_callers_cannot_mix_the_same_run_identity(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    plan = _plan(project, "example:check")
    def invoke():
        try:
            return project.run(plan, run_id="4" * 32)
        except BaseException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = tuple(executor.map(lambda _index: invoke(), range(2)))

    assert sum(isinstance(value, RunResult) for value in values) == 1
    assert sum(isinstance(value, FileExistsError) for value in values) == 1
    assert _read_run(project, "example:check", "4" * 32).status == "succeeded"


def test_project_import_does_not_load_tool_capability_modules() -> None:
    script = """
import json, sys
import sigilicon.project
print(json.dumps(sorted(name for name in sys.modules if name.startswith('sigilicon'))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    modules = json.loads(completed.stdout)

    assert len(modules) <= 20
    assert not any(
        name.startswith((
            "sigilicon.virtuoso",
            "sigilicon.workflows.synopsys",
            "sigilicon.capabilities",
        ))
        for name in modules
    )
