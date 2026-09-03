"""Generic direct Spectre execution under the repository process supervisor.

Design code supplies a rendered deck and its measurement contract.  This
module owns only executable discovery, immutable input handling, invocation,
native-log checks, and raw-output existence checks.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from sigilicon.artifacts import read_nofollow_text
from sigilicon.execution._model import Resources
from sigilicon.execution._workspace import StepWorkspace
from sigilicon.external_tools import (
    ProcessPort,
    ProcessRequest,
    managed_process,
    owned_directory,
    owned_input_closure,
    owned_input_file,
    spectre_env,
)


_SPECTRE_ZERO_ERRORS = re.compile(r"spectre completes with\s+0 errors", re.IGNORECASE)
@dataclass(frozen=True)
class SpectreExecution:
    """Evidence from one direct Spectre process-group invocation."""

    executable: Path
    canonical_deck: Path
    invocation_deck: Path
    stdout_log: Path
    stderr_log: Path
    native_log: Path | None
    raw_outputs: Mapping[str, Path]


@dataclass(frozen=True)
class StagedSpectreInput:
    """One immutable file copied into a run artifact before tool launch."""

    key: str
    source: Path
    components: tuple[str, ...]


@dataclass(frozen=True)
class SpectreRunResult:
    """Decision and measurement artifact from one Spectre contract."""

    measurements: Path
    passed: bool


class MeasurementContractFailure(RuntimeError):
    """The simulator ran, but a machine-verifiable declared contract failed."""

    def __init__(self, message: str, result: SpectreRunResult) -> None:
        super().__init__(message)
        self.result = result


def run_spectre_deck(
    record: StepWorkspace,
    *,
    render_deck: Callable[[Mapping[str, str]], str],
    inputs: Mapping[str, Path],
    output_names: Sequence[str],
    timeout: int,
    resources: Resources,
    process: ProcessPort = managed_process,
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
    )
    canonical_deck.chmod(0o444)
    executable = resources.require_tool("cadence.spectre")
    work_dir = record.directory("work")
    completed = None
    invocation_deck: Path | None = None
    with (
        resources.owned_tool("cadence.spectre") as owned_spectre,
        owned_directory(work_dir) as owned_work,
        owned_input_closure(
            input_root,
            files=tuple(inputs.values()),
        ) as owned_inputs,
    ):
        tool_paths = {
            key: owned_inputs.child(
                Path(os.path.abspath(path)).relative_to(input_root)
            )
            for key, path in inputs.items()
        }
        invocation_deck = record.write_text(
            "work",
            ("spectre.tool.scs",),
            render_deck(tool_paths),
        )
        invocation_deck.chmod(0o444)
        with owned_input_file(invocation_deck) as owned_deck:
            command = (
                *owned_spectre.command,
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
            )

            def validate_spawn() -> None:
                owned_spectre.require_visible()
                owned_inputs.require_visible()
                owned_deck.require_visible()

            pass_fds = tuple(
                dict.fromkeys(
                    (
                        owned_work.fd,
                        owned_inputs.fd,
                        owned_spectre.target.fd,
                        owned_spectre.target.directory_fd,
                        owned_deck.fd,
                        owned_deck.directory_fd,
                    )
                )
            )
            completed = process.run(ProcessRequest(
                argv=tuple(command),
                executable=owned_spectre.executable,
                cwd=Path(owned_work.child_path),
                environment=spectre_env(executable, resources.environment),
                timeout_seconds=timeout,
                before_spawn=validate_spawn,
                pass_fds=pass_fds,
            ))

    assert completed is not None
    stdout_log = record.write_text(
        "logs",
        ("spectre.stdout.log",),
        completed.stdout,
    )
    stderr_log = record.write_text(
        "logs",
        ("spectre.stderr.log",),
        completed.stderr,
    )
    native_log_candidate = record.path("work", "spectre.out")
    native_log: Path | None = None
    proof_sources = [completed.stdout, completed.stderr]
    if native_log_candidate.is_file():
        native_log = record.copy_file(
            "logs",
            ("spectre.out",),
            native_log_candidate,
        )
        proof_sources.append(read_nofollow_text(native_log, errors="replace"))
    if completed.returncode != 0:
        tail = "\n".join(
            f"{completed.stdout}\n{completed.stderr}".splitlines()[-80:]
        )
        raise RuntimeError(f"Spectre exited {completed.returncode}\n{tail}")
    if not _SPECTRE_ZERO_ERRORS.search("\n".join(proof_sources)):
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
        stderr_log=stderr_log,
        native_log=native_log,
        raw_outputs=raw_outputs,
    )


def _stage_inputs(
    record: StepWorkspace,
    inputs: Sequence[StagedSpectreInput],
) -> Mapping[str, Path]:
    staged: dict[str, Path] = {}
    for item in inputs:
        if not item.key or item.key in staged or not item.components:
            raise ValueError("Spectre staged input keys and destination components must be unique")
        if len(item.components) > 1:
            record.directory("inputs", *item.components[:-1])
        staged[item.key] = record.copy_file("inputs", item.components, item.source)
    return staged


def run_spectre_measurement(
    *,
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
    artifacts: StepWorkspace,
    resources: Resources,
    process: ProcessPort = managed_process,
) -> SpectreRunResult:
    """Execute one design-defined contract using only shared flow mechanics.

    The caller defines its deck, parser, normalizer, and measurement decision;
    this generic workflow stages immutable inputs and invokes the guarded
    Spectre runner. The caller must supply files owned by its current Step.
    """

    staged = _stage_inputs(artifacts, inputs)
    artifacts.write_json(
        "inputs",
        ("external-input-references.json",),
        dict(external_input_references),
    )
    execution = run_spectre_deck(
        artifacts,
        render_deck=render,
        inputs=staged,
        output_names=(output_name,),
        timeout=timeout,
        resources=resources,
        process=process,
    )
    raw = artifacts.copy_file(
        "outputs",
        (raw_result_name,),
        execution.raw_outputs[output_name],
    )
    parsed = parse(read_nofollow_text(raw, errors="strict"))
    artifacts.write_text(
        "outputs",
        (normalized_name,),
        normalize(parsed),
    )
    payload = dict(evaluate(parsed))
    payload.setdefault("contract_version", 1)
    payload.setdefault("condition", dict(condition))
    passed = payload.get("passed")
    if type(passed) is not bool:
        raise ValueError("Spectre measurement must contain a boolean 'passed' field")
    measurements = artifacts.write_json(
        "outputs",
        ("measurements.json",),
        payload,
    )
    artifacts.add_file("work", artifacts.directory("work"))
    result = SpectreRunResult(
        measurements=measurements,
        passed=passed,
    )
    if not result.passed:
        raise MeasurementContractFailure("machine measurement contract failed", result)
    return result
