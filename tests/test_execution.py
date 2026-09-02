from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sigilicon.execution import (
    Artifact,
    Adapter,
    ContractError,
    ExecutionError,
    Step,
    PreflightCheck,
    RunResult,
    Source,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.execution.model import ResourceBinding, Resources
from sigilicon.execution.operations import parse_selector
from sigilicon.execution.runs import RunStoreError, RunStore
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
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

    for name in (
        "Step",
        "Adapter",
        "ExecutionPlan",
        "RunResult",
        "RunStore",
    ):
        assert getattr(execution, name).__name__ == name
    for removed in (
        "OperationStep",
        "PreparedStep",
        "Backend",
        "Operation",
        "Preparation",
        "_RunStore",
        "Resources",
    ):
        assert not hasattr(execution, removed)
    assert "_read_run" not in Project.__dict__
    assert "_clean_run" not in Project.__dict__


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


def test_step_files_does_not_import_the_execution_model() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/sigilicon/execution/step_files.py"
    ).read_text(encoding="utf-8")

    assert "sigilicon.execution.model" not in source


@pytest.fixture(autouse=True)
def _bind_test_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    global _TEST_ADAPTERS
    _TEST_ADAPTERS = ()
    monkeypatch.setattr(
        "sigilicon.backends.trusted_adapters",
        lambda: _TEST_ADAPTERS,
    )


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
        """schema = 2
contract_kind = "ip-component"
path_scope = "owner"
owner = "example"
name = "example"
kind = "rtl-ip"
operation_catalog = "ip/example/configs/operations.toml"

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
        output = context.write_text("source", "value.txt", str(step.request["text"]))
        return StepResult.succeeded(
            artifacts=(Artifact("source", "text.plain", output),),
            facts={"length": len(str(step.request["text"]))},
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
    assert plan.steps[0].request == {"text": "hello"}
    assert plan.steps[0].evidence.record == {
        "role": "regression",
        "level": "l0",
        "scope": "source",
    }
    assert json.loads(json.dumps(plan.record))["schema"] == 9
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


def test_project_plan_is_independent_of_runtime_resources(
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
    assert without_runtime.preflight(current).status == "blocked"
    with pytest.raises(ContractError, match="not produced by this Project"):
        without_runtime.preflight(plan)


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
                "json.load(sys.stdin)).record, sort_keys=True))"
            ),
            str(tmp_path),
        ),
        check=True,
        capture_output=True,
        input=json.dumps(local.record),
        text=True,
    )

    assert json.loads(completed.stdout) == local.record


def test_execution_plan_record_restoration_requires_current_sources(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    record = project.plan("example:check").record
    operations.write_text("invalid = true\n", encoding="utf-8")

    with pytest.raises((ContractError, ValueError)):
        project.plan(json.loads(json.dumps(record)))


def test_execution_plan_record_rejects_modified_evidence(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    record = project.plan("example:check").record
    record["steps"][0]["evidence"]["role"] = []

    with pytest.raises(ValueError, match="current project closure"):
        project.plan(record)


def test_cli_preflight_consumes_a_portable_plan_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sigilicon.cli.main import main as sigilicon_main

    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(project.plan("example:check").record))

    assert sigilicon_main(
        (
            "flow",
            "preflight",
            "--plan-file",
            str(plan_path),
            "--project-root",
            str(tmp_path),
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_cli_run_consumes_a_portable_plan_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sigilicon.cli.main import main as sigilicon_main

    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(project.plan("example:check").record))

    assert sigilicon_main(
        (
            "flow",
            "run",
            "--plan-file",
            str(plan_path),
            "--project-root",
            str(tmp_path),
            "--run-id",
            "6" * 32,
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


def test_cli_rejects_a_symlinked_plan_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sigilicon.cli.main import main as sigilicon_main

    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter())
    target = tmp_path / "target-plan.json"
    target.write_text(json.dumps(project.plan("example:check").record))
    plan_path = tmp_path / "plan.json"
    plan_path.symlink_to(target)

    assert sigilicon_main(
        (
            "flow",
            "preflight",
            "--plan-file",
            str(plan_path),
            "--project-root",
            str(tmp_path),
        )
    ) == 2
    assert "cannot read execution plan" in capsys.readouterr().err


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
        """schema = 2
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
    assert not result.run_root.exists()
    with pytest.raises(RunStoreError):
        _read_run(project, "example:all", "a" * 32)


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
    original = RunStore._validate_inventory
    calls = 0

    def replace_after_validation(_cls, paths, manifest):
        nonlocal calls
        original(paths, manifest)
        calls += 1
        if calls == 3:
            outputs = paths.role("outputs")
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

    monkeypatch.setattr(RunStore, "_validate_inventory", replace_after_validation)

    _clean_run(project, "example:check", result.run_id)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not result.run_root.exists()


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
    assert result.run_root == (
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


def test_adapter_preflight_cannot_hide_source_replacement(tmp_path: Path) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class MutatingBackend(CopyAdapter):
        def preflight(self, step, resources):
            source.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return ()

    project = _project(tmp_path, MutatingBackend(),)
    plan = _plan(project, "example:check")

    with pytest.raises(ExecutionError, match="changed immediately before adapter"):
        project.run(plan, run_id="b" * 32)
    failed = _read_run(project, "example:check", "b" * 32)
    assert failed.record["contract_kind"] == "run-failure"
    assert failed.status == "failed"
    _clean_run(project, "example:check", "b" * 32)


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
    sealed = result.run_root / "inputs/sources/configs/value.txt"
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
    persisted = (result.run_root / "inputs/execution-plan.json").read_text(
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
        (result.run_root / "inputs/runtime-bindings.json").read_text()
    )
    assert bindings["configuration"]["tools"] == {"test.tool": "/bin/true"}
    assert bindings["configuration"]["values"] == {
        "test.value": "configured"
    }
    assert bindings["resources"][0]["kind"] == "file"
    assert str(live) not in str(bindings)
    store, owner, operation, variant = _run_store_call(project, "example:check")
    selected = store._select(
        owner=owner,
        operation=operation,
        variant=variant,
        run_id=result.run_id,
    )
    store._validate_runtime_bindings(selected.paths, bindings)
    bindings["resources"][0]["size"] += 1
    with pytest.raises(RunStoreError, match="resource identity drift"):
        store._validate_runtime_bindings(selected.paths, bindings)


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
        (result.run_root / "inputs/runtime-bindings.json").read_text()
    )
    assert bindings["resources"][0]["kind"] == "directory"
    assert bindings["resources"][0]["directories"] == ["nested", "nested/empty"]


def test_directory_resource_rejects_symlink_members(tmp_path: Path) -> None:
    live = tmp_path / "library"
    live.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"target")
    (live / "link").symlink_to(target)

    with pytest.raises(ContractError, match="must not contain symlinks"):
        ResourceBinding.capture(live, identity="pdk:fixture/library")


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
    with pytest.raises(ContractError, match="mapping"):
        Step("bad", "fake.copy", "not-a-mapping")  # type: ignore[arg-type]
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
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
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
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
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
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
        "failed"
    )
    assert result.outcomes[0].result.artifacts[0].path.read_text() == "{}\n"
    restored = _read_run(project, "example:check", result.run_id)
    assert restored.outcomes[0].result.facts == {"passed": False}
    artifact = restored.outcomes[0].result.artifacts[0]
    assert artifact.role == "evidence"
    assert artifact.path.read_text() == "{}\n"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    artifact.record_path.unlink()
    artifact.record_path.symlink_to(outside)
    assert artifact.path.read_text() == "{}\n"
    assert restored.record["steps"][0]["artifacts"][0]["path"] == (
        "outputs/run/evidence/failure.json"
    )


def test_failure_after_a_completed_step_records_partial_provenance(tmp_path: Path) -> None:
    _write_project(tmp_path)
    live = tmp_path / "ip/example/configs/value.txt"

    class DriftingCopyAdapter(CopyAdapter):
        def run(self, context: StepContext, step: Step) -> StepResult:
            result = super().run(context, step)
            live.write_text("changed\n", encoding="utf-8")
            return result

    project = _project(tmp_path, DriftingCopyAdapter(), UpperAdapter())
    plan = _plan(project, "example:all")

    with pytest.raises(ExecutionError, match="changed immediately before adapter"):
        project.run(
            plan,
            run_id="5" * 32,
        )
    stored = _read_run(project, "example:all", "5" * 32)
    assert stored.status == "partial"
    assert stored.provenance["completed_steps"] == ("source",)


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

    result_path = result.run_root / "outputs/run-result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["variant"] = "tampered"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunStoreError, match="manifest|result"):
        _read_run(project, "example:check", result.run_id)


def test_run_store_rejects_same_size_artifact_tampering(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyAdapter(),)
    plan = _plan(project, "example:check")
    result = project.run(
        plan,
        run_id="9" * 32,
    )
    output = result.outcomes[0].result.artifacts[0].path
    output.write_text("jello", encoding="utf-8")

    with pytest.raises(RunStoreError, match="metadata"):
        _read_run(project, "example:check", result.run_id)


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
