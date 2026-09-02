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
    ContractError,
    ExecutionError,
    Operation,
    Step,
    PreflightCheck,
    Resources,
    RunResult,
    Source,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.execution.backend import Preparation
from sigilicon.execution.model import ExternalResource
from sigilicon.execution.operations import parse_selector
from sigilicon.execution.runs import RunStoreError, RunStore
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
from sigilicon.project import Project


_TEST_BACKENDS: tuple[object, ...] = ()


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
        "Operation",
        "Step",
        "Backend",
        "ExecutionPlan",
        "RunResult",
        "RunStore",
    ):
        assert getattr(execution, name).__name__ == name
    for removed in (
        "OperationStep",
        "PreparedStep",
        "_Backend",
        "_BackendRegistry",
        "_Preparation",
        "_RunStore",
    ):
        assert not hasattr(execution, removed)
    assert "_read_run" not in Project.__dict__
    assert "_clean_run" not in Project.__dict__


def test_step_files_does_not_import_the_execution_model() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/sigilicon/execution/step_files.py"
    ).read_text(encoding="utf-8")

    assert "sigilicon.execution.model" not in source


@pytest.fixture(autouse=True)
def _bind_test_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    global _TEST_BACKENDS
    _TEST_BACKENDS = ()
    monkeypatch.setattr(
        "sigilicon.backends.trusted_backends",
        lambda: _TEST_BACKENDS,
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
""",
        encoding="utf-8",
    )
    (root / "catalogs/ip.toml").write_text(
        """schema = 1
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "test"

[targets]
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
        """schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "example"
name = "example"
kind = "rtl-ip"
operation_catalog = "ip/example/configs/operations.toml"

[filesets]
operation_catalog = ["ip/example/configs/operations.toml"]
value = ["ip/example/configs/value.txt"]
""",
        encoding="utf-8",
    )
    (owner / "configs/value.txt").write_text("hello\n", encoding="utf-8")
    operations = owner / "configs/operations.toml"
    operations.write_text(
        """schema = 2
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"

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


class CopyBackend:
    name = "fake.copy"

    def prepare(self, _project, step):
        return Preparation(Step.from_operation(step))

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


class UpperBackend:
    name = "fake.upper"

    def prepare(self, _project, step):
        return Preparation(Step.from_operation(step))

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


def _project(root: Path, *backends) -> Project:
    """Select test-only implementations through pytest's private assembly."""

    global _TEST_BACKENDS
    _TEST_BACKENDS = backends
    return Project.open(root)


def test_project_plan_is_source_bound_and_preflight_has_no_side_effects(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(), UpperBackend())

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
    assert json.loads(json.dumps(plan.record))["schema"] == 4
    assert "backend_bindings" not in plan.record
    assert not hasattr(plan, "_backends")
    assert not project.artifact_root.exists()
    assert project.preflight(plan).status == "blocked"
    checked = project.preflight(plan, Resources(frozenset({"offline"})))
    assert checked.status == "ready"
    assert not project.artifact_root.exists()

    operations.write_text(operations.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert project.preflight(plan, Resources(frozenset({"offline"}))).status == "blocked"


def test_backend_discovered_sources_have_canonical_plan_order(tmp_path: Path) -> None:
    _write_project(tmp_path)
    owner = tmp_path / "ip/example"
    first = owner / "configs/discovered-a.txt"
    second = owner / "configs/discovered-b.txt"
    first.write_text("a\n", encoding="utf-8")
    second.write_text("b\n", encoding="utf-8")

    class DiscoveringBackend(CopyBackend):
        def __init__(self, paths: tuple[Path, ...]) -> None:
            self.paths = paths

        def prepare(self, project, step):
            owner_root = project.owner("example").root
            sources = tuple(
                Source.capture(path, root=owner_root, scope="owner")
                for path in self.paths
            )
            names = tuple(sorted(source.path for source in sources))
            return Preparation(
                Step.from_operation(
                    step, sources=tuple(dict.fromkeys((*step.sources, *names)))
                ),
                sources,
            )

    forward = _project(
        tmp_path,
        DiscoveringBackend((first, second)),
    ).plan("example:check")
    reverse = _project(
        tmp_path,
        DiscoveringBackend((second, first)),
    ).plan("example:check")

    assert forward.identity == reverse.identity
    assert forward.steps[0].sources[-2:] == (
        "configs/discovered-a.txt",
        "configs/discovered-b.txt",
    )


def test_backend_preparation_rejects_a_compiled_source_change(tmp_path: Path) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class ChangingBackend(CopyBackend):
        def prepare(self, project, step):
            source.write_text("changed during prepare\n", encoding="utf-8")
            return super().prepare(project, step)

    project = _project(tmp_path, ChangingBackend())

    with pytest.raises(ContractError, match="changed during backend preparation"):
        project.plan("example:check")


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
        """schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "foreign"
name = "foreign"
kind = "rtl-ip"

[filesets]
source = ["ip/foreign/value.txt"]
""",
        encoding="utf-8",
    )
    value = foreign / "value.txt"
    value.write_text("foreign\n", encoding="utf-8")

    class ForeignSourceBackend(CopyBackend):
        def prepare(self, project, step):
            source = Source.capture(value, root=foreign, scope="owner")
            return Preparation(
                Step.from_operation(step, sources=(*step.sources, source.path)),
                (source,),
            )

    project = _project(tmp_path, ForeignSourceBackend())
    with pytest.raises(ContractError, match="source owned by 'foreign'"):
        project.plan("example:check")


def test_backend_cannot_discover_a_symlinked_source(tmp_path: Path) -> None:
    _write_project(tmp_path)
    owner = tmp_path / "ip/example/configs"
    target = owner / "real.txt"
    target.write_text("real\n", encoding="utf-8")
    link = owner / "linked.txt"
    link.symlink_to(target.name)

    class SymlinkSourceBackend(CopyBackend):
        def prepare(self, project, step):
            source = Source.capture(link, root=owner.parent, scope="owner")
            return Preparation(
                Step.from_operation(step, sources=(*step.sources, source.path)),
                (source,),
            )

    project = _project(tmp_path, SymlinkSourceBackend())
    with pytest.raises(ContractError, match="non-symlink"):
        project.plan("example:check")


def test_operation_catalog_rejects_source_groups(tmp_path: Path) -> None:
    operations = _write_project(tmp_path)
    operations.write_text(
        operations.read_text(encoding="utf-8")
        + '\n[source_groups]\nlegacy = ["configs/value.txt"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="unknown owner operation fields"):
        Project.open(tmp_path).plan("example:check")


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
        _project(tmp_path, CopyBackend()).plan("example:check")


def test_operation_rejects_legacy_target_selector(tmp_path: Path) -> None:
    _write_project(tmp_path)
    with pytest.raises(ContractError, match=r"owner:operation\[@variant\]"):
        Project.open(tmp_path).plan("example/smoke:check")


def test_component_rejects_legacy_target_catalog_field(tmp_path: Path) -> None:
    _write_project(tmp_path)
    component = tmp_path / "ip/example/component.toml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            "operation_catalog =", "target_catalog ="
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="target_catalog was removed"):
        Project.open(tmp_path)


def test_project_runs_dag_and_run_store_validates_and_cleans_result(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(), UpperBackend())
    plan = project.plan("example:all")
    progress: list[tuple[str, str]] = []

    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
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
    project = _project(tmp_path, CopyBackend(),)
    result = project.run(
        project.plan("example:check"),
        Resources(frozenset({"offline"})),
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
    project = _project(tmp_path, CopyBackend())
    plan = project.plan("example:check@fast")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
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
    with pytest.raises(ContractError, match="unknown trusted backend"):
        project.plan("example:check")


def test_backend_preflight_cannot_hide_source_replacement(tmp_path: Path) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class MutatingBackend(CopyBackend):
        def preflight(self, step, resources):
            source.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return ()

    project = _project(tmp_path, MutatingBackend(),)
    plan = project.plan("example:check")

    with pytest.raises(ExecutionError, match="changed immediately before backend"):
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

    class SealedSourceBackend(CopyBackend):
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
    plan = project.plan("example:check")

    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="f" * 32,
    )

    assert result.outcomes[0].result.artifacts[0].path.read_text() == "hello\n"
    sealed = result.run_root / "inputs/sources/configs/value.txt"
    assert sealed.stat().st_mode & 0o777 == 0o444
    assert sealed.parent.stat().st_mode & 0o777 == 0o555


def test_external_resource_is_sealed_without_persisting_location_or_text(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "site/pdk/model.scs"
    live.parent.mkdir(parents=True)
    live.write_text("proprietary model\n", encoding="utf-8")

    class ResourceBackend(CopyBackend):
        def prepare(self, _project, step):
            resource = ExternalResource.capture(
                live,
                identity="pdk:fixture:simulation/nominal/model.scs",
            )
            prepared = Step.from_operation(
                step,
                resources=(resource.identity,),
            )
            return Preparation(prepared, resources=(resource,))

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

    project = _project(tmp_path, ResourceBackend())
    plan = project.plan("example:check")

    assert str(live) not in str(plan.record)
    assert "proprietary model" not in str(plan.record)
    assert plan.record["resources"] == [plan.resources[0].record]

    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="e" * 32,
    )

    output = result.outcomes[0].result.artifacts[0].path
    assert output.read_text(encoding="utf-8") == "proprietary model\n"
    persisted = (result.run_root / "inputs/execution-plan.json").read_text(
        encoding="utf-8"
    )
    assert str(live) not in persisted
    assert "proprietary model" not in persisted


def test_external_resource_reader_rejects_sealed_content_tampering(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "site/pdk/model.scs"
    live.parent.mkdir(parents=True)
    live.write_text("trusted model\n", encoding="utf-8")

    class TamperingBackend(CopyBackend):
        def prepare(self, _project, step):
            resource = ExternalResource.capture(
                live,
                identity="pdk:fixture:simulation/nominal/model.scs",
            )
            return Preparation(
                Step.from_operation(
                    step,
                    resources=(resource.identity,),
                ),
                resources=(resource,),
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

    project = _project(tmp_path, TamperingBackend())
    plan = project.plan("example:check")

    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="7" * 32,
    )
    assert result.outcomes[0].result.facts == {"tamper_rejected": True}


def test_project_rejects_a_plan_for_another_composition(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example:check")
    forged = replace(plan, project_identity="sha256-" + "0" * 64)

    with pytest.raises(ValueError, match="not authorized by this Project"):
        project.preflight(forged, Resources(frozenset({"offline"})))


def test_project_rejects_an_authorized_plan_modified_by_the_caller(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend())
    plan = project.plan("example:check")
    forged_step = replace(plan.steps[0], request={"text": "forged"})
    forged = replace(plan, steps=(forged_step,))

    with pytest.raises(ValueError, match="not authorized by this Project"):
        project.preflight(forged, Resources(frozenset({"offline"})))


def test_project_rejects_composition_source_drift(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend())
    plan = project.plan("example:check")
    manifest = tmp_path / "sigilicon.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    checked = project.preflight(plan, Resources(frozenset({"offline"})))
    assert checked.status == "blocked"
    assert checked.checks[0].record == {
        "kind": "project",
        "subject": "example",
        "status": "blocked",
        "detail": "project composition changed after planning",
    }


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
        Operation("bad", "fake.copy", "not-a-mapping")  # type: ignore[arg-type]
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

    class ExtraOutputBackend(CopyBackend):
        def preflight(self, step, resources):
            return ()

        def run(self, context: StepContext, step: Step) -> StepResult:
            published = context.write_text("source", "published.txt", "published")
            context.write_text("source", "extra.txt", "extra")
            return StepResult.succeeded(
                artifacts=(Artifact("source", "text.plain", published),)
            )

    project = _project(tmp_path, ExtraOutputBackend(),)
    plan = project.plan("example:check")

    with pytest.raises(ExecutionError, match="output inventory"):
        project.run(plan, run_id="c" * 32)


def test_uncertain_execution_is_distinct_from_closed_result_storage(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class UncertainBackend(CopyBackend):
        def run(self, context: StepContext, step: Step) -> StepResult:
            return StepResult.uncertain("descendant cleanup could not be proven")

    project = _project(tmp_path, UncertainBackend(),)
    plan = project.plan("example:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
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

    class CleanupUnknownBackend(CopyBackend):
        def run(self, context: StepContext, step: Step) -> StepResult:
            raise ProcessGroupCleanupUncertainError(
                "descendant cleanup could not be proven"
            )

    project = _project(tmp_path, CleanupUnknownBackend(),)
    plan = project.plan("example:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="2" * 32,
    )

    assert result.status == "uncertain"
    assert result.outcomes[0].result.message == (
        "descendant cleanup could not be proven"
    )


def test_cancelled_execution_is_closed_and_restorable(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class CancelledBackend(CopyBackend):
        def run(self, context: StepContext, step: Step) -> StepResult:
            return StepResult.cancelled("operator cancelled the tool")

    project = _project(tmp_path, CancelledBackend(),)
    plan = project.plan("example:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="3" * 32,
    )

    assert result.status == "cancelled"
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
        "cancelled"
    )
    assert _read_run(project, "example:check", result.run_id).status == "cancelled"


def test_failed_step_keeps_its_diagnostic_evidence(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class RejectingBackend(CopyBackend):
        def run(self, context: StepContext, step: Step) -> StepResult:
            evidence = context.write_text("evidence", "failure.json", "{}\n")
            return StepResult(
                "failed",
                (Artifact("evidence", "evidence.failure", evidence),),
                {"passed": False},
                "qualification failed",
            )

    project = _project(tmp_path, RejectingBackend(),)
    plan = project.plan("example:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="4" * 32,
    )

    assert result.status == "failed"
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
        "failed"
    )
    assert result.outcomes[0].result.artifacts[0].path.read_text() == "{}\n"
    restored = _read_run(project, "example:check", result.run_id)
    assert restored.outcomes[0].result.facts == {"passed": False}
    assert restored.outcomes[0].result.artifacts[0].role == "evidence"


def test_failure_after_a_completed_step_records_partial_provenance(tmp_path: Path) -> None:
    _write_project(tmp_path)
    live = tmp_path / "ip/example/configs/value.txt"

    class DriftingCopyBackend(CopyBackend):
        def run(self, context: StepContext, step: Step) -> StepResult:
            result = super().run(context, step)
            live.write_text("changed\n", encoding="utf-8")
            return result

    project = _project(tmp_path, DriftingCopyBackend(), UpperBackend())
    plan = project.plan("example:all")

    with pytest.raises(ExecutionError, match="changed immediately before backend"):
        project.run(
            plan,
            Resources(frozenset({"offline"})),
            run_id="5" * 32,
        )
    stored = _read_run(project, "example:all", "5" * 32)
    assert stored.status == "partial"
    assert stored.provenance["completed_steps"] == ("source",)


def test_run_store_is_independent_of_current_operation_source_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
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
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="9" * 32,
    )
    output = result.outcomes[0].result.artifacts[0].path
    output.write_text("jello", encoding="utf-8")

    with pytest.raises(RunStoreError, match="metadata"):
        _read_run(project, "example:check", result.run_id)


def test_run_identity_is_exclusive(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example:check")
    resources = Resources(frozenset({"offline"}))
    project.run(plan, resources, run_id="e" * 32)

    with pytest.raises(FileExistsError):
        project.run(plan, resources, run_id="e" * 32)
    assert _read_run(project, "example:check", "e" * 32).status == "succeeded"


def test_concurrent_callers_cannot_mix_the_same_run_identity(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example:check")
    resources = Resources(frozenset({"offline"}))

    def invoke():
        try:
            return project.run(plan, resources, run_id="4" * 32)
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
