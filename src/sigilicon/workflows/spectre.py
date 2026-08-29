"""Generic direct Spectre execution under the repository process supervisor.

Design code supplies a rendered deck and its measurement contract.  This
module owns only executable discovery, immutable input handling, invocation,
native-log checks, and raw-output existence checks.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Mapping, Sequence

from sigilicon.artifacts import ArtifactRecord, new_identity, read_nofollow_text
from sigilicon.domain.repository import Project
from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    process_group_cleanup_uncertainty,
    run_process_group,
)
from sigilicon.paths import ProjectContext
from sigilicon.virtuoso.operation_journal import write_operation_incident
from sigilicon.workflows.source_control import artifact_source_state
from sigilicon.workflows.virtuoso_operations import export_project_netlist


_SPECTRE_ZERO_ERRORS = re.compile(r"spectre completes with\s+0 errors", re.IGNORECASE)


@dataclass(frozen=True)
class SpectreExecution:
    """Evidence from one direct Spectre process-group invocation."""

    executable: Path
    canonical_deck: Path
    invocation_deck: Path
    stdout_log: Path
    native_log: Path | None
    raw_outputs: Mapping[str, Path]


@dataclass(frozen=True, init=False)
class SpectreArtifactContext:
    """Repository and design coordinates for a direct-Spectre run."""

    project: Project = field(repr=False, compare=False, hash=False)
    library: str
    cell: str
    testbench: str
    _project_root: Path = field(init=False, repr=False)

    def __init__(
        self,
        project_root: Path | None = None,
        library: str | None = None,
        cell: str | None = None,
        testbench: str | None = None,
        *,
        project: Project | None = None,
    ) -> None:
        """Bind an explicit Project or one legacy positional project root."""

        repository = Project.bind(project=project, project_root=project_root)
        if library is None or cell is None or testbench is None:
            raise ValueError("Spectre artifact coordinates must be explicit")
        object.__setattr__(self, "project", repository)
        object.__setattr__(self, "library", library)
        object.__setattr__(self, "cell", cell)
        object.__setattr__(self, "testbench", testbench)
        object.__setattr__(self, "_project_root", repository.project_root)

    @property
    def project_root(self) -> Path:
        """Compatibility path view of the canonical Project."""

        return self._project_root


@dataclass(frozen=True)
class StagedSpectreInput:
    """One immutable file copied into a run artifact before tool launch."""

    key: str
    source: Path
    components: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class OaSpectreNetlistExport:
    """One exact bridge-exported OA Spectre package, never a guessed latest run."""

    input_scs: Path
    support_files: tuple[Path, ...]
    manifest_path: Path


@dataclass(frozen=True)
class SpectreRunResult:
    """Stable artifact references for one direct Spectre measurement contract."""

    kind: str
    run_id: str
    run_dir: Path
    manifest_path: Path
    measurements: Path
    waveform: Path | None
    raw_curve: Path | None
    passed: bool
    condition: Mapping[str, object]


class MeasurementContractFailure(RuntimeError):
    """The simulator ran, but a machine-verifiable declared contract failed."""

    def __init__(self, message: str, result: SpectreRunResult) -> None:
        super().__init__(message)
        self.result = result


def find_spectre(explicit: Path | None = None) -> Path:
    """Find the Cadence launcher without resolving its wrapper symlink.

    Cadence's public ``spectre`` launcher may be a symlink whose resolved
    target expects its original bin-directory layout.  Returning the absolute
    launcher path rather than ``Path.resolve()`` preserves that contract.
    """

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    configured = os.environ.get("VB_SPECTRE_BIN")
    if configured:
        candidates.append(Path(configured))
    discovered = shutil.which("spectre")
    if discovered:
        candidates.append(Path(discovered))
    mmsim = os.environ.get("MMSIM")
    if mmsim:
        candidates.append(Path(mmsim) / "bin" / "spectre")
    for candidate in candidates:
        absolute = Path(os.path.abspath(candidate))
        if absolute.is_file() and os.access(absolute, os.X_OK):
            return absolute
    if explicit is not None:
        raise FileNotFoundError(f"spectre does not exist or is not executable: {explicit}")
    raise FileNotFoundError("spectre was not found; configure VB_SPECTRE_BIN or MMSIM")


def run_spectre_deck(
    record: ArtifactRecord,
    *,
    render_deck: Callable[[Mapping[str, str]], str],
    inputs: Mapping[str, Path],
    output_names: Sequence[str],
    timeout: int,
    spectre: Path | None = None,
) -> SpectreExecution:
    """Render, execute, and prove one direct Spectre deck.

    ``inputs`` must already be immutable files in the caller's artifact
    ``inputs`` role.  Keys are only render placeholders; their values are
    re-bound to exact inherited descriptors for the executable invocation.
    This lets included netlists keep relative support-file references while
    preserving the existing artifact and supervisor safety contracts.
    """

    if timeout <= 0:
        raise ValueError("Spectre timeout must be positive")
    if not inputs:
        raise ValueError("Spectre invocation requires at least one input")
    if not output_names or len(set(output_names)) != len(output_names):
        raise ValueError("Spectre output names must be non-empty and unique")
    for name in output_names:
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError(f"invalid Spectre output name: {name!r}")

    input_root = record.paths.role("inputs")
    canonical_paths: dict[str, str] = {}
    for key, path in inputs.items():
        if not key:
            raise ValueError("Spectre input keys must be non-empty")
        absolute = Path(os.path.abspath(path))
        if not absolute.is_relative_to(input_root.resolve()):
            raise RuntimeError("Spectre inputs must be materialized in this artifact")
        if not absolute.is_file():
            raise FileNotFoundError(f"declared Spectre input does not exist: {absolute}")
        canonical_paths[key] = str(absolute)

    canonical_deck = record.write_text(
        "inputs",
        ("spectre.scs",),
        render_deck(canonical_paths),
        label="rendered Spectre deck",
    )
    canonical_deck.chmod(0o444)
    executable = find_spectre(spectre)
    work_dir = record.paths.role("work")
    completed = None
    invocation_deck: Path | None = None
    with owned_directory(work_dir) as owned_work, ExitStack() as resources:
        owned_inputs = {
            key: resources.enter_context(owned_input_file(path))
            for key, path in inputs.items()
        }
        tool_paths = {
            key: owned.child_named_path for key, owned in owned_inputs.items()
        }
        invocation_deck = record.write_text(
            "work",
            ("spectre.tool.scs",),
            render_deck(tool_paths),
            label="invocation-only exact-inode Spectre deck",
        )
        invocation_deck.chmod(0o444)
        owned_deck = resources.enter_context(owned_input_file(invocation_deck))
        command = (
            str(executable),
            "-64",
            "-format",
            "psfascii",
            "-raw",
            owned_work.child_file("psf"),
            owned_deck.child_named_path,
        )
        record.write_json(
            "inputs",
            ("simulator-command.json",),
            {
                "argv": list(command),
                "cwd": str(Path(owned_work.child_path)),
                "timeout_seconds": timeout,
                "canonical_deck": str(canonical_deck),
                "note": "argv is the exact guarded Spectre process invocation; /proc paths bind immutable open descriptors and are intentionally ephemeral after completion",
            },
            label="exact Spectre process command",
        )

        def validate_spawn() -> None:
            for owned in (*owned_inputs.values(), owned_deck):
                owned.require_visible()

        pass_fds = tuple(
            dict.fromkeys(
                (
                    owned_work.fd,
                    owned_deck.fd,
                    owned_deck.directory_fd,
                    *(
                        descriptor
                        for owned in owned_inputs.values()
                        for descriptor in (owned.fd, owned.directory_fd)
                    ),
                )
            )
        )
        completed = run_process_group(
            command,
            cwd=Path(owned_work.child_path),
            env=cadence_subprocess_env(),
            timeout=timeout,
            before_spawn=validate_spawn,
            pass_fds=pass_fds,
        )

    assert completed is not None
    stdout_log = record.write_text(
        "logs",
        ("spectre.stdout.log",),
        completed.stdout,
        label="Spectre captured stdout",
    )
    native_log_candidate = record.path("work", "spectre.out")
    native_log: Path | None = None
    log_text = completed.stdout
    if native_log_candidate.is_file():
        native_log = record.copy_file(
            "logs",
            ("spectre.out",),
            native_log_candidate,
            label="Spectre native log",
        )
        log_text = read_nofollow_text(native_log, errors="replace")
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-80:])
        raise RuntimeError(f"Spectre exited {completed.returncode}\n{tail}")
    if not _SPECTRE_ZERO_ERRORS.search(log_text + "\n" + completed.stdout):
        raise RuntimeError("could not prove a Spectre completion with zero errors")
    raw_outputs: dict[str, Path] = {}
    for name in output_names:
        output = record.path("work", *Path(name).parts)
        if not output.is_file():
            raise RuntimeError(f"Spectre did not produce declared direct-print output {name}")
        raw_outputs[name] = output
    return SpectreExecution(
        executable=executable,
        canonical_deck=canonical_deck,
        invocation_deck=invocation_deck,
        stdout_log=stdout_log,
        native_log=native_log,
        raw_outputs=raw_outputs,
    )


def export_oa_spectre_netlist(
    client: Any,
    *,
    paths: ProjectContext,
    library: str,
    testbench: str,
    timeout: int = 120,
) -> OaSpectreNetlistExport:
    """Bridge-export an OA testbench plus every direct relative support file."""

    exported = export_project_netlist(
        client,
        paths,
        library,
        testbench,
        view="schematic",
        simulator="spectre",
        timeout=timeout,
    )
    if not exported.manifest_path.is_file():
        raise RuntimeError("OA netlist export is not inside a complete artifact")
    return OaSpectreNetlistExport(
        input_scs=exported.input_scs,
        support_files=exported.support_files,
        manifest_path=exported.manifest_path,
    )


def _terminalize_spectre_record(
    project: Project,
    record: ArtifactRecord,
    error: BaseException,
) -> None:
    if record.status != "running":
        return
    try:
        record.add_file(
            "work", record.paths.role("work"), label="native Spectre work directory"
        )
    except Exception:
        pass
    cleanup_reason = process_group_cleanup_uncertainty(error)
    if cleanup_reason is None:
        record.fail(error)
        return
    reason = "Spectre process-group cleanup could not be proven: " + cleanup_reason
    try:
        incident = write_operation_incident(
            workspace_root=project.workspace_root,
            artifact_root=project.artifact_root,
            operation_id=str(record.manifest["operation_id"]),
            name="direct-spectre",
            policy="isolated-process-group",
            status="uncertain",
            error=error,
            uncertain_reason=reason,
            view_snapshots=(),
            ownership_scopes=({"kind": "spectre-process-group"},),
        )
        record.attach_incident(incident)
    except Exception as incident_error:
        record.fail(
            error,
            uncertain_reason=reason,
            details={
                "incident_recording_error": (
                    f"{type(incident_error).__name__}: {incident_error}"
                )[:4000]
            },
        )
    else:
        record.fail(error, uncertain_reason=reason)


def _stage_inputs(
    record: ArtifactRecord,
    inputs: Sequence[StagedSpectreInput],
) -> Mapping[str, Path]:
    staged: dict[str, Path] = {}
    for item in inputs:
        if not item.key or item.key in staged or not item.components:
            raise ValueError("Spectre staged input keys and destination components must be unique")
        if len(item.components) > 1:
            record.directory("inputs", *item.components[:-1])
        staged[item.key] = record.copy_file(
            "inputs", item.components, item.source, label=item.label
        )
    return staged


def run_spectre_measurement(
    context: SpectreArtifactContext,
    *,
    kind: str,
    condition: Mapping[str, object],
    inputs: Sequence[StagedSpectreInput],
    external_input_references: Mapping[str, Any],
    render: Callable[[Mapping[str, str]], str],
    output_name: str,
    raw_result_name: str,
    parse: Callable[[str], Any],
    normalize: Callable[[Any], str],
    normalized_name: str,
    evaluate: Callable[[Any], Mapping[str, object]],
    timeout: int,
    artifact_root: Path | None = None,
    spectre: Path | None = None,
) -> SpectreRunResult:
    """Execute one design-defined contract using only shared flow mechanics.

    The caller defines its deck, parser, normalizer, and measurement decision;
    this generic workflow creates the unique artifact, stages immutable inputs,
    invokes the guarded Spectre runner, and terminalizes success/failure.
    """

    project = (
        context.project
        if artifact_root is None
        else context.project.with_artifact_root(artifact_root)
    )
    record = ArtifactRecord.begin(
        project.artifacts.execution(
            owner=context.library,
            target=context.testbench,
            flow="spectre",
            variant=kind,
            identity=new_identity(),
            artifact_kind="standalone_simulation",
            identity_kind="run_id",
        ),
        entities={
            "library": context.library,
            "cell": context.cell,
            "testbench": context.testbench,
        },
        operation="direct-spectre-characterization",
        backend="spectre",
        source=artifact_source_state(context.project_root),
    )
    record.bind_operation(new_identity())
    try:
        staged = _stage_inputs(record, inputs)
        record.write_json(
            "inputs",
            ("external-input-references.json",),
            dict(external_input_references),
            label="attested external characterization inputs",
        )
        execution = run_spectre_deck(
            record,
            render_deck=render,
            inputs=staged,
            output_names=(output_name,),
            timeout=timeout,
            spectre=spectre,
        )
        raw = record.copy_file(
            "outputs", (raw_result_name,), execution.raw_outputs[output_name], label="raw Spectre direct-print data"
        )
        parsed = parse(read_nofollow_text(raw, errors="strict"))
        normalized = record.write_text(
            "outputs", (normalized_name,), normalize(parsed), label="normalized direct-print curve data"
        )
        payload = dict(evaluate(parsed))
        payload.setdefault("contract_version", 1)
        payload.setdefault("condition", dict(condition))
        measurements = record.write_json(
            "outputs", ("measurements.json",), payload, label="machine-verifiable measurement contract"
        )
        record.add_file("work", record.paths.role("work"), label="native Spectre work directory")
        result = SpectreRunResult(
            kind=kind,
            run_id=record.paths.identity,
            run_dir=record.paths.root,
            manifest_path=record.paths.manifest,
            measurements=measurements,
            waveform=normalized,
            raw_curve=raw,
            passed=bool(payload.get("passed")),
            condition=dict(condition),
        )
        if not result.passed:
            raise MeasurementContractFailure("machine measurement contract failed", result)
        details: dict[str, Any] = {
            "kind": kind,
            "condition": dict(condition),
            "spectre": str(execution.executable),
        }
        record.succeed(completion_evidence=(measurements,), details=details)
        return result
    except BaseException as error:
        _terminalize_spectre_record(project, record, error)
        raise


def run_spectre_multi_measurement(
    context: SpectreArtifactContext,
    *,
    kind: str,
    condition: Mapping[str, object],
    inputs: Sequence[StagedSpectreInput],
    external_input_references: Mapping[str, Any],
    render: Callable[[Mapping[str, str]], str],
    outputs: Mapping[str, tuple[str, ...]],
    evaluate: Callable[[Mapping[str, Path]], Mapping[str, object]],
    timeout: int,
    artifact_root: Path | None = None,
    spectre: Path | None = None,
) -> SpectreRunResult:
    """Run one Spectre deck with multiple declared raw-output files.

    This is the multi-analysis sibling of :func:`run_spectre_measurement`.
    ``outputs`` maps the tool-relative output path to its immutable destination
    below ``outputs/``.  The evaluator receives only those copied result files;
    it never reads mutable simulator work files.
    """

    if not outputs:
        raise ValueError("multi-output Spectre measurement requires outputs")
    if len(set(outputs.values())) != len(outputs):
        raise ValueError("multi-output Spectre destinations must be unique")
    project = (
        context.project
        if artifact_root is None
        else context.project.with_artifact_root(artifact_root)
    )
    record = ArtifactRecord.begin(
        project.artifacts.execution(
            owner=context.library,
            target=context.testbench,
            flow="spectre",
            variant=kind,
            identity=new_identity(),
            artifact_kind="standalone_simulation",
            identity_kind="run_id",
        ),
        entities={
            "library": context.library,
            "cell": context.cell,
            "testbench": context.testbench,
        },
        operation="direct-spectre-characterization",
        backend="spectre",
        source=artifact_source_state(context.project_root),
    )
    record.bind_operation(new_identity())
    try:
        staged = _stage_inputs(record, inputs)
        record.write_json(
            "inputs",
            ("external-input-references.json",),
            dict(external_input_references),
            label="attested external characterization inputs",
        )
        execution = run_spectre_deck(
            record,
            render_deck=render,
            inputs=staged,
            output_names=tuple(outputs),
            timeout=timeout,
            spectre=spectre,
        )
        copied: dict[str, Path] = {}
        for tool_name, destination in outputs.items():
            if not destination:
                raise ValueError("multi-output result destination cannot be empty")
            if len(destination) > 1:
                record.directory("outputs", *destination[:-1])
            copied[tool_name] = record.copy_file(
                "outputs",
                destination,
                execution.raw_outputs[tool_name],
                label=f"raw Spectre output {tool_name}",
            )
        payload = dict(evaluate(copied))
        payload.setdefault("contract_version", 1)
        payload.setdefault("condition", dict(condition))
        measurements = record.write_json(
            "outputs",
            ("measurements.json",),
            payload,
            label="machine-verifiable multi-analysis measurement contract",
        )
        record.add_file("work", record.paths.role("work"), label="native Spectre work directory")
        result = SpectreRunResult(
            kind=kind,
            run_id=record.paths.identity,
            run_dir=record.paths.root,
            manifest_path=record.paths.manifest,
            measurements=measurements,
            waveform=None,
            raw_curve=None,
            passed=bool(payload.get("passed")),
            condition=dict(condition),
        )
        if not result.passed:
            raise MeasurementContractFailure("machine measurement contract failed", result)
        record.succeed(
            completion_evidence=(measurements,),
            details={
                "kind": kind,
                "condition": dict(condition),
                "spectre": str(execution.executable),
            },
        )
        return result
    except BaseException as error:
        _terminalize_spectre_record(project, record, error)
        raise


def publish_measurement_summary(
    context: SpectreArtifactContext,
    *,
    kind: str,
    condition: Mapping[str, object],
    inputs: Sequence[StagedSpectreInput],
    result_files: Mapping[str, str],
    payload: Mapping[str, object],
    artifact_root: Path | None = None,
) -> SpectreRunResult:
    """Publish a self-contained derived sweep or distribution result."""

    project = (
        context.project
        if artifact_root is None
        else context.project.with_artifact_root(artifact_root)
    )
    record = ArtifactRecord.begin(
        project.artifacts.execution(
            owner=context.library,
            target=context.testbench,
            flow="spectre-derived",
            variant=kind,
            identity=new_identity(),
            artifact_kind="standalone_simulation",
            identity_kind="run_id",
        ),
        entities={
            "library": context.library,
            "cell": context.cell,
            "testbench": context.testbench,
        },
        operation="characterization-analysis",
        backend="analysis",
        source=artifact_source_state(context.project_root),
    )
    record.bind_operation(new_identity())
    try:
        _stage_inputs(record, inputs)
        exact_command = condition.get("exact_command")
        if isinstance(exact_command, Mapping):
            record.write_json(
                "inputs",
                ("analysis-command.json",),
                dict(exact_command),
                label="exact characterization campaign command",
            )
        for name, content in result_files.items():
            record.write_text("outputs", (name,), content, label=f"derived {kind} result")
        measurements = record.write_json(
            "outputs", ("measurements.json",), payload, label="derived measurement contract"
        )
        result = SpectreRunResult(
            kind=kind,
            run_id=record.paths.identity,
            run_dir=record.paths.root,
            manifest_path=record.paths.manifest,
            measurements=measurements,
            waveform=None,
            raw_curve=None,
            passed=bool(payload.get("passed")),
            condition=dict(condition),
        )
        if not result.passed:
            raise MeasurementContractFailure("derived measurement contract failed", result)
        record.succeed(
            completion_evidence=(measurements,),
            details={"kind": kind},
        )
        return result
    except BaseException as error:
        _terminalize_spectre_record(project, record, error)
        raise
