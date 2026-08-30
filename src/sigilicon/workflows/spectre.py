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

from sigilicon.artifacts import new_identity, read_nofollow_text
from sigilicon.domain.repository import Project
from sigilicon.external_tools import (
    cadence_subprocess_env,
    owned_directory,
    owned_input_file,
    run_process_group,
)
from sigilicon.paths import ProjectContext
from sigilicon.workflows.run_artifacts import (
    RunArtifacts,
    managed_run_artifacts_from_environment,
    scoped_run_artifacts,
)
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


@dataclass(frozen=True, eq=False)
class SpectreArtifactContext:
    """Repository and design coordinates for a direct-Spectre run."""

    project: Project = field(repr=False, compare=False, hash=False)
    library: str
    cell: str
    testbench: str

    def _comparison_key(self) -> tuple[Path, str, str, str]:
        return (self.project.project_root, self.library, self.cell, self.testbench)

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        assert isinstance(other, SpectreArtifactContext)
        return self._comparison_key() == other._comparison_key()

    def __hash__(self) -> int:
        return hash(self._comparison_key())


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
    record: RunArtifacts,
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

    input_root = record.directory("inputs")
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
    work_dir = record.directory("work")
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


def _stage_inputs(
    record: RunArtifacts,
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


def _measurement_artifacts(
    *,
    artifacts: RunArtifacts | None,
) -> RunArtifacts:
    if artifacts is not None:
        return artifacts
    inherited = managed_run_artifacts_from_environment()
    if inherited is None:
        raise RuntimeError(
            "Spectre measurements require artifacts owned by a parent Flow Action"
        )
    return scoped_run_artifacts(
        inherited,
        f"spectre-{new_identity()}",
    )


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
    artifacts: RunArtifacts | None = None,
    spectre: Path | None = None,
) -> SpectreRunResult:
    """Execute one design-defined contract using only shared flow mechanics.

    The caller defines its deck, parser, normalizer, and measurement decision;
    this generic workflow stages immutable inputs and invokes the guarded
    Spectre runner.  A supplied or inherited ``RunArtifacts`` keeps the work
    inside its parent Flow Action. Direct callers outside a Flow must supply
    the parent's artifact workspace explicitly.
    """

    del context
    run_artifacts = _measurement_artifacts(
        artifacts=artifacts,
    )
    staged = _stage_inputs(run_artifacts, inputs)
    run_artifacts.write_json(
        "inputs",
        ("external-input-references.json",),
        dict(external_input_references),
        label="attested external characterization inputs",
    )
    execution = run_spectre_deck(
        run_artifacts,
        render_deck=render,
        inputs=staged,
        output_names=(output_name,),
        timeout=timeout,
        spectre=spectre,
    )
    raw = run_artifacts.copy_file(
        "outputs",
        (raw_result_name,),
        execution.raw_outputs[output_name],
        label="raw Spectre direct-print data",
    )
    parsed = parse(read_nofollow_text(raw, errors="strict"))
    normalized = run_artifacts.write_text(
        "outputs",
        (normalized_name,),
        normalize(parsed),
        label="normalized direct-print curve data",
    )
    payload = dict(evaluate(parsed))
    payload.setdefault("contract_version", 1)
    payload.setdefault("condition", dict(condition))
    measurements = run_artifacts.write_json(
        "outputs",
        ("measurements.json",),
        payload,
        label="machine-verifiable measurement contract",
    )
    run_artifacts.add_file(
        "work",
        run_artifacts.directory("work"),
        label="native Spectre work directory",
    )
    result = SpectreRunResult(
        kind=kind,
        run_id=run_artifacts.run_id,
        run_dir=run_artifacts.root,
        manifest_path=run_artifacts.root / "run_manifest.json",
        measurements=measurements,
        waveform=normalized,
        raw_curve=raw,
        passed=bool(payload.get("passed")),
        condition=dict(condition),
    )
    if not result.passed:
        raise MeasurementContractFailure("machine measurement contract failed", result)
    return result


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
    artifacts: RunArtifacts | None = None,
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
    del context
    run_artifacts = _measurement_artifacts(
        artifacts=artifacts,
    )
    staged = _stage_inputs(run_artifacts, inputs)
    run_artifacts.write_json(
        "inputs",
        ("external-input-references.json",),
        dict(external_input_references),
        label="attested external characterization inputs",
    )
    execution = run_spectre_deck(
        run_artifacts,
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
            run_artifacts.directory("outputs", *destination[:-1])
        copied[tool_name] = run_artifacts.copy_file(
            "outputs",
            destination,
            execution.raw_outputs[tool_name],
            label=f"raw Spectre output {tool_name}",
        )
    payload = dict(evaluate(copied))
    payload.setdefault("contract_version", 1)
    payload.setdefault("condition", dict(condition))
    measurements = run_artifacts.write_json(
        "outputs",
        ("measurements.json",),
        payload,
        label="machine-verifiable multi-analysis measurement contract",
    )
    run_artifacts.add_file(
        "work",
        run_artifacts.directory("work"),
        label="native Spectre work directory",
    )
    result = SpectreRunResult(
        kind=kind,
        run_id=run_artifacts.run_id,
        run_dir=run_artifacts.root,
        manifest_path=run_artifacts.root / "run_manifest.json",
        measurements=measurements,
        waveform=None,
        raw_curve=None,
        passed=bool(payload.get("passed")),
        condition=dict(condition),
    )
    if not result.passed:
        raise MeasurementContractFailure("machine measurement contract failed", result)
    return result


def publish_measurement_summary(
    context: SpectreArtifactContext,
    *,
    kind: str,
    condition: Mapping[str, object],
    inputs: Sequence[StagedSpectreInput],
    result_files: Mapping[str, str],
    payload: Mapping[str, object],
    artifacts: RunArtifacts | None = None,
) -> SpectreRunResult:
    """Publish a derived sweep or distribution result in its parent Flow."""

    del context
    run_artifacts = _measurement_artifacts(artifacts=artifacts)
    _stage_inputs(run_artifacts, inputs)
    exact_command = condition.get("exact_command")
    if isinstance(exact_command, Mapping):
        run_artifacts.write_json(
            "inputs",
            ("analysis-command.json",),
            dict(exact_command),
            label="exact characterization campaign command",
        )
    for name, content in result_files.items():
        run_artifacts.write_text(
            "outputs",
            (name,),
            content,
            label=f"derived {kind} result",
        )
    measurements = run_artifacts.write_json(
        "outputs",
        ("measurements.json",),
        payload,
        label="derived measurement contract",
    )
    result = SpectreRunResult(
        kind=kind,
        run_id=run_artifacts.run_id,
        run_dir=run_artifacts.root,
        manifest_path=run_artifacts.root / "run_manifest.json",
        measurements=measurements,
        waveform=None,
        raw_curve=None,
        passed=bool(payload.get("passed")),
        condition=dict(condition),
    )
    if not result.passed:
        raise MeasurementContractFailure("derived measurement contract failed", result)
    return result
