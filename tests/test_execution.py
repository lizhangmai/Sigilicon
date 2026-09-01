from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from sigilicon.execution import (
    Artifact,
    Backends,
    ContractError,
    ExecutionError,
    PreflightCheck,
    Resources,
    RunResult,
    RunStoreError,
    Step,
    StepContext,
    StepOutcome,
    StepResult,
)
from sigilicon.external_tools import ProcessGroupCleanupUncertainError
from sigilicon.project import Project


_TEST_BACKENDS: tuple[object, ...] = ()


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
target_catalog = "ip/example/configs/operations.toml"

[filesets]
flow = ["ip/example/configs/operations.toml", "ip/example/configs/value.txt"]
""",
        encoding="utf-8",
    )
    (owner / "configs/value.txt").write_text("hello\n", encoding="utf-8")
    operations = owner / "configs/operations.toml"
    operations.write_text(
        """schema = 1
contract_kind = "owner-operations"
path_scope = "owner"
owner = "example"

[source_groups]
value = ["configs/value.txt"]

[targets.smoke]
description = "Offline execution smoke"
with = { prefix = "value" }
source_groups = ["value"]
operations = ["check", "all"]

[operations.check]
uses = "fake.copy"
with = { text = "hello" }
evidence = { role = "regression", level = "l0", scope = "source" }

[operations.all]

[[operations.all.steps]]
id = "source"
uses = "fake.copy"
with = { text = "hello" }

[[operations.all.steps]]
id = "transform"
uses = "fake.upper"
needs = ["source"]
""",
        encoding="utf-8",
    )
    return operations


class CopyBackend:
    name = "fake.copy"

    def preflight(self, step, resources):
        return (
            PreflightCheck(
                "capability",
                "offline",
                "ready" if "offline" in resources.capabilities else "blocked",
            ),
        )

    def run(self, context: StepContext) -> StepResult:
        output = context.write_text("source", "value.txt", str(context.step.config["text"]))
        return StepResult.succeeded(
            artifacts=(Artifact("source", "text.plain", output),),
            facts={"length": len(str(context.step.config["text"]))},
        )


class UpperBackend:
    name = "fake.upper"

    def preflight(self, step, resources):
        return ()

    def run(self, context: StepContext) -> StepResult:
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

    plan = project.plan("example/smoke:check")

    assert plan.owner == "example"
    assert plan.target == "smoke"
    assert plan.operation == "check"
    assert [step.uses for step in plan.steps] == ["fake.copy"]
    assert plan.steps[0].config == {"prefix": "value", "text": "hello"}
    assert plan.steps[0].evidence.record == {
        "role": "regression",
        "level": "l0",
        "scope": "source",
    }
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
            self.binding_sources = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths
            }

        def bind(self, project, step):
            return self

    forward = _project(
        tmp_path,
        DiscoveringBackend((first, second)),
    ).plan("example/smoke:check")
    reverse = _project(
        tmp_path,
        DiscoveringBackend((second, first)),
    ).plan("example/smoke:check")

    assert forward.identity == reverse.identity
    assert forward.steps[0].sources[-2:] == (
        "configs/discovered-a.txt",
        "configs/discovered-b.txt",
    )


def test_backend_binding_rejects_a_compiled_source_change(tmp_path: Path) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class ChangingBackend(CopyBackend):
        def bind(self, project, step):
            source.write_text("changed during bind\n", encoding="utf-8")
            return self

    project = _project(tmp_path, ChangingBackend())

    with pytest.raises(ContractError, match="changed during backend binding"):
        project.plan("example/smoke:check")


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
        binding_sources = {
            value: hashlib.sha256(value.read_bytes()).hexdigest(),
        }

        def bind(self, project, step):
            return self

    project = _project(tmp_path, ForeignSourceBackend())
    with pytest.raises(ContractError, match="source owned by 'foreign'"):
        project.plan("example/smoke:check")


def test_backend_cannot_discover_a_symlinked_source(tmp_path: Path) -> None:
    _write_project(tmp_path)
    owner = tmp_path / "ip/example/configs"
    target = owner / "real.txt"
    target.write_text("real\n", encoding="utf-8")
    link = owner / "linked.txt"
    link.symlink_to(target.name)

    class SymlinkSourceBackend(CopyBackend):
        binding_sources = {
            link: hashlib.sha256(target.read_bytes()).hexdigest(),
        }

        def bind(self, project, step):
            return self

    project = _project(tmp_path, SymlinkSourceBackend())
    with pytest.raises(ContractError, match="symlinked source"):
        project.plan("example/smoke:check")


def test_operation_rejects_an_unknown_source_group(tmp_path: Path) -> None:
    operations = _write_project(tmp_path)
    operations.write_text(
        operations.read_text(encoding="utf-8").replace(
            'source_groups = ["value"]',
            'source_groups = ["missing"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="unknown group 'missing'"):
        Project.open(tmp_path).plan("example/smoke:check")


def test_operation_globs_capture_owner_and_shared_project_sources(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    (tmp_path / "ip/example/rtl").mkdir()
    (tmp_path / "ip/example/rtl/design.sv").write_text(
        "module design; endmodule\n", encoding="utf-8"
    )
    (tmp_path / "configs/platform/tool.toml").write_text(
        "tool = 'fixture'\n", encoding="utf-8"
    )
    operations.write_text(
        operations.read_text(encoding="utf-8").replace(
            '[operations.check]\nuses = "fake.copy"',
            '[operations.check]\n'
            'uses = "fake.copy"\n'
            'source_globs = ["rtl/**/*.sv"]\n'
            'project_source_globs = ["configs/platform/**/*.toml"]',
        ),
        encoding="utf-8",
    )

    plan = _project(tmp_path, CopyBackend()).plan("example/smoke:check")
    sources = {(source.scope, source.path) for source in plan.sources}

    assert ("owner", "rtl/design.sv") in sources
    assert ("project", "configs/platform/catalog.toml") in sources
    assert ("project", "configs/platform/tool.toml") in sources


def test_source_group_can_share_owner_and_project_globs(tmp_path: Path) -> None:
    operations = _write_project(tmp_path)
    (tmp_path / "ip/example/rtl").mkdir()
    (tmp_path / "ip/example/rtl/design.sv").write_text(
        "module design; endmodule\n", encoding="utf-8"
    )
    operations.write_text(
        operations.read_text(encoding="utf-8").replace(
            '[source_groups]\nvalue = ["configs/value.txt"]',
            '[source_groups.value]\n'
            'sources = ["configs/value.txt"]\n'
            'source_globs = ["rtl/**/*.sv"]\n'
            'project_source_globs = ["configs/platform/**/*.toml"]',
        ),
        encoding="utf-8",
    )

    plan = _project(tmp_path, CopyBackend()).plan("example/smoke:check")
    sources = {(source.scope, source.path) for source in plan.sources}

    assert ("owner", "configs/value.txt") in sources
    assert ("owner", "rtl/design.sv") in sources
    assert ("project", "configs/platform/catalog.toml") in sources


def test_project_runs_dag_and_run_store_validates_and_cleans_result(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(), UpperBackend())
    plan = project.plan("example/smoke:all")
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
    stored = project.runs.read(
        owner="example",
        target="smoke",
        operation="all",
        run_id="a" * 32,
    )
    assert stored.status == "succeeded"
    assert stored.plan_identity == plan.identity

    project.runs.clean(
        owner="example",
        target="smoke",
        operation="all",
        run_id="a" * 32,
    )
    assert not result.run_root.exists()
    with pytest.raises(RunStoreError):
        project.runs.read(
            owner="example",
            target="smoke",
            operation="all",
            run_id="a" * 32,
        )


def test_missing_backend_blocks_preflight_and_run(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = Project.open(tmp_path)
    with pytest.raises(ContractError, match="unknown trusted backend"):
        project.plan("example/smoke:check")


def test_backend_preflight_cannot_hide_source_replacement(tmp_path: Path) -> None:
    _write_project(tmp_path)
    source = tmp_path / "ip/example/configs/value.txt"

    class MutatingBackend(CopyBackend):
        def preflight(self, step, resources):
            source.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return ()

    project = _project(tmp_path, MutatingBackend(),)
    plan = project.plan("example/smoke:check")

    with pytest.raises(ExecutionError, match="changed immediately before backend"):
        project.run(plan, run_id="b" * 32)
    failed = project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id="b" * 32,
    )
    assert failed.record["contract_kind"] == "run-failure"
    assert failed.status == "failed"
    project.runs.clean(
        owner="example",
        target="smoke",
        operation="check",
        run_id="b" * 32,
    )


def test_backend_consumes_the_sealed_source_not_the_live_owner_file(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    live = tmp_path / "ip/example/configs/value.txt"

    class SealedSourceBackend(CopyBackend):
        def run(self, context: StepContext) -> StepResult:
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
    plan = project.plan("example/smoke:check")

    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="f" * 32,
    )

    assert result.outcomes[0].result.artifacts[0].path.read_text() == "hello\n"
    sealed = result.run_root / "inputs/sources/configs/value.txt"
    assert sealed.stat().st_mode & 0o777 == 0o444
    assert sealed.parent.stat().st_mode & 0o777 == 0o555


def test_project_rejects_a_plan_for_another_composition(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example/smoke:check")
    forged = replace(plan, project_identity="sha256-" + "0" * 64)

    with pytest.raises(ValueError, match="not authorized by this Project"):
        project.preflight(forged, Resources(frozenset({"offline"})))


def test_project_rejects_an_authorized_plan_modified_by_the_caller(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend())
    plan = project.plan("example/smoke:check")
    forged_step = replace(plan.steps[0], config={"text": "forged"})
    forged = replace(plan, steps=(forged_step,))

    with pytest.raises(ValueError, match="not authorized by this Project"):
        project.preflight(forged, Resources(frozenset({"offline"})))


def test_project_rejects_a_replaced_backend_binding(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend())
    plan = project.plan("example/smoke:check")
    forged = replace(plan, _backends={plan.steps[0].id: UpperBackend()})

    with pytest.raises(ValueError, match="not authorized by this Project"):
        project.preflight(forged, Resources(frozenset({"offline"})))


def test_project_rejects_composition_source_drift(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend())
    plan = project.plan("example/smoke:check")
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
    class InvalidBackend(CopyBackend):
        name = "Invalid/Backend"

    with pytest.raises(ContractError, match="canonical identity"):
        Backends((InvalidBackend(),))
    with pytest.raises(ContractError, match="mapping"):
        Step("bad", "fake.copy", "not-a-mapping")  # type: ignore[arg-type]
    outcome = StepOutcome("run", "fake.copy", StepResult.succeeded())
    with pytest.raises(ContractError, match="disagrees"):
        RunResult(
            "example",
            "smoke",
            "check",
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

        def run(self, context: StepContext) -> StepResult:
            published = context.write_text("source", "published.txt", "published")
            context.write_text("source", "extra.txt", "extra")
            return StepResult.succeeded(
                artifacts=(Artifact("source", "text.plain", published),)
            )

    project = _project(tmp_path, ExtraOutputBackend(),)
    plan = project.plan("example/smoke:check")

    with pytest.raises(ExecutionError, match="output inventory"):
        project.run(plan, run_id="c" * 32)


def test_uncertain_execution_is_distinct_from_closed_result_storage(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class UncertainBackend(CopyBackend):
        def run(self, context: StepContext) -> StepResult:
            return StepResult.uncertain("descendant cleanup could not be proven")

    project = _project(tmp_path, UncertainBackend(),)
    plan = project.plan("example/smoke:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="6" * 32,
    )

    assert result.status == "uncertain"
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
        "uncertain"
    )
    restored = project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id=result.run_id,
    )
    assert restored.status == "uncertain"


def test_process_cleanup_uncertainty_cannot_be_downgraded_to_failure(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)

    class CleanupUnknownBackend(CopyBackend):
        def run(self, context: StepContext) -> StepResult:
            raise ProcessGroupCleanupUncertainError(
                "descendant cleanup could not be proven"
            )

    project = _project(tmp_path, CleanupUnknownBackend(),)
    plan = project.plan("example/smoke:check")
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
        def run(self, context: StepContext) -> StepResult:
            return StepResult.cancelled("operator cancelled the tool")

    project = _project(tmp_path, CancelledBackend(),)
    plan = project.plan("example/smoke:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="3" * 32,
    )

    assert result.status == "cancelled"
    assert json.loads((result.run_root / "manifest.json").read_text())["status"] == (
        "cancelled"
    )
    assert project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id=result.run_id,
    ).status == "cancelled"


def test_failed_step_keeps_its_diagnostic_evidence(tmp_path: Path) -> None:
    _write_project(tmp_path)

    class RejectingBackend(CopyBackend):
        def run(self, context: StepContext) -> StepResult:
            evidence = context.write_text("evidence", "failure.json", "{}\n")
            return StepResult(
                "failed",
                (Artifact("evidence", "evidence.failure", evidence),),
                {"passed": False},
                "qualification failed",
            )

    project = _project(tmp_path, RejectingBackend(),)
    plan = project.plan("example/smoke:check")
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
    restored = project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id=result.run_id,
    )
    assert restored.outcomes[0].result.facts == {"passed": False}
    assert restored.outcomes[0].result.artifacts[0].role == "evidence"


def test_failure_after_a_completed_step_records_partial_provenance(tmp_path: Path) -> None:
    _write_project(tmp_path)
    live = tmp_path / "ip/example/configs/value.txt"

    class DriftingCopyBackend(CopyBackend):
        def run(self, context: StepContext) -> StepResult:
            result = super().run(context)
            live.write_text("changed\n", encoding="utf-8")
            return result

    project = _project(tmp_path, DriftingCopyBackend(), UpperBackend())
    plan = project.plan("example/smoke:all")

    with pytest.raises(ExecutionError, match="changed immediately before backend"):
        project.run(
            plan,
            Resources(frozenset({"offline"})),
            run_id="5" * 32,
        )
    stored = project.runs.read(
        owner="example",
        target="smoke",
        operation="all",
        run_id="5" * 32,
    )
    assert stored.status == "partial"
    assert stored.provenance["completed_steps"] == ("source",)


def test_run_store_is_independent_of_current_operation_source_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    operations = _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example/smoke:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="d" * 32,
    )
    operations.unlink()

    stored = project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id=result.run_id,
    )
    assert stored.status == "succeeded"

    result_path = result.run_root / "outputs/run-result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["target"] = "tampered"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunStoreError, match="manifest|result"):
        project.runs.read(
            owner="example",
            target="smoke",
            operation="check",
            run_id=result.run_id,
        )


def test_run_store_rejects_same_size_artifact_tampering(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example/smoke:check")
    result = project.run(
        plan,
        Resources(frozenset({"offline"})),
        run_id="9" * 32,
    )
    output = result.outcomes[0].result.artifacts[0].path
    output.write_text("jello", encoding="utf-8")

    with pytest.raises(RunStoreError, match="metadata"):
        project.runs.read(
            owner="example",
            target="smoke",
            operation="check",
            run_id=result.run_id,
        )


def test_run_identity_is_exclusive(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example/smoke:check")
    resources = Resources(frozenset({"offline"}))
    project.run(plan, resources, run_id="e" * 32)

    with pytest.raises(FileExistsError):
        project.run(plan, resources, run_id="e" * 32)
    assert project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id="e" * 32,
    ).status == "succeeded"


def test_concurrent_callers_cannot_mix_the_same_run_identity(tmp_path: Path) -> None:
    _write_project(tmp_path)
    project = _project(tmp_path, CopyBackend(),)
    plan = project.plan("example/smoke:check")
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
    assert project.runs.read(
        owner="example",
        target="smoke",
        operation="check",
        run_id="4" * 32,
    ).status == "succeeded"


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
