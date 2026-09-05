"""Source-owned measurements around the managed direct Spectre boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from sigilicon.adapters.cadence._common import (
    _positive_integer, _relative, _strict_config, _text,
)
from sigilicon.adapters.cadence.spectre import run_spectre_deck
from sigilicon.contracts import freeze_toml_document, thaw_toml_document
from sigilicon.domain.platform import load_platform, model_resource_identities
from sigilicon.execution import AdapterPreparation
from sigilicon.execution._model import (
    Artifact, ContractError, ExecutionIO, ResourceBinding, Resources, Source, Step, StepResult,
)
from sigilicon.external_tools import (
    ProcessRequest, managed_process, owned_sealed_input, process_group_cleanup_uncertainty,
)
from sigilicon.project import Project


@dataclass(frozen=True)
class MeasurementAction:
    program: str
    spec: str
    circuit: str
    parameters: Mapping[str, object]
    models: tuple[tuple[str, str], ...]
    sections: tuple[str, ...]
    timeout_seconds: int

    @property
    def record(self) -> dict[str, object]:
        return {
            "kind": "spectre-measurement", "program": self.program,
            "spec": self.spec, "circuit": self.circuit,
            "parameters": thaw_toml_document(self.parameters),
            "models": [list(row) for row in self.models], "sections": list(self.sections),
            "timeout_seconds": self.timeout_seconds,
        }


def prepare_measurement(project: Project, step: Step, resources: Resources) -> AdapterPreparation:
    config = _strict_config(step, frozenset({
        "program", "spec", "circuit", "platform", "model_set", "parameters", "timeout_seconds",
    }))
    selected = {}
    for field in ("program", "spec", "circuit"):
        value = _relative(_text(config, field), field)
        if value not in step.sources:
            raise ContractError(f"measurement {field} must be selected by the step filesets")
        selected[field] = value
    parameters = config.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise ContractError("measurement parameters must be a table")
    platform = load_platform(project, _text(config, "platform"), resources=resources)
    if platform.simulation is None:
        raise ContractError("Spectre measurement requires a simulation platform")
    model_name = _text(config, "model_set")
    try:
        models = platform.simulation.model_sets[model_name]
    except KeyError as exc:
        raise ContractError(f"unknown platform model set: {model_name}") from exc
    identities = model_resource_identities(platform, models)
    model_inputs = tuple((identities[path], path.name) for path in models.paths)
    if len({name for _, name in model_inputs}) != len(model_inputs):
        raise ContractError("Spectre model support filenames collide")
    action = MeasurementAction(
        **selected, parameters=freeze_toml_document(parameters), models=model_inputs,
        sections=tuple(models.sections), timeout_seconds=_positive_integer(config, "timeout_seconds"),
    )
    return AdapterPreparation(
        action=action,
        sources=tuple(Source.capture(path, root=project.project_root, scope="project")
                      for path in platform.source_documents),
        resources=(
            resources.capture("cadence.spectre"), resources.capture("runtime.python"),
            *(ResourceBinding.capture(path, identity=identities[path]) for path in models.paths),
        ),
    )


def _invoke(context: ExecutionIO, action: MeasurementAction, request: dict[str, object]) -> dict:
    encoded = json.dumps({
        "source_name": action.program, "source_text": context.source_text(action.program),
        "request": request,
    }, allow_nan=False).encode()
    with owned_sealed_input(encoded, name="measurement.json") as source, context.runtime.owned_tool("runtime.python") as python:
        completed = managed_process.run(ProcessRequest(
            argv=(*python.command, "-m", "sigilicon.adapters.cadence._measurement_worker", source.child_path),
            executable=python.executable,
            cwd=context.work_directory,
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            timeout_seconds=action.timeout_seconds,
            pass_fds=(source.fd, python.target.fd, python.target.directory_fd),
            before_spawn=python.require_visible,
        ))
    if completed.returncode:
        raise RuntimeError(f"measurement program exited {completed.returncode}: {completed.stderr}")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise ValueError("measurement program returned a non-object result")
    return result


def run_measurement(context: ExecutionIO) -> StepResult:
    action = context.step.action
    if not isinstance(action, MeasurementAction):
        raise ContractError("Spectre measurement requires its compiled action")
    context.step.validate_action()
    workspace = context.workspace("spectre", action.record)
    artifacts: list[Artifact] = []
    request = {
        "spec": context.source_text(action.spec),
        "circuit": context.source_text(action.circuit),
        "parameters": thaw_toml_document(action.parameters),
        "sections": list(action.sections),
    }
    try:
        generation = _invoke(context, action, {**request, "phase": "render"})
        if set(generation) != {"deck", "outputs", "condition"}:
            raise ValueError("measurement generation requires deck, outputs and condition")
        template = generation["deck"]
        outputs = generation["outputs"]
        condition = generation["condition"]
        if not isinstance(template, str) or not isinstance(condition, dict):
            raise ValueError("invalid measurement deck or conditions")
        if not isinstance(outputs, list) or not outputs:
            raise ValueError("measurement generation requires raw output names")
        outputs = tuple(_relative(name, "measurement output") for name in outputs)
        workspace.directory("inputs", "models")
        staged = {
            "circuit": workspace.copy_file("inputs", ("circuit.scs",), context.source_path(action.circuit)),
        }
        for index, (identity, name) in enumerate(action.models):
            staged["model" if index == 0 else f"support_{index}"] = workspace.copy_file(
                "inputs", ("models", name), context.resource_path(identity),
            )
        def render(paths: Mapping[str, str]) -> str:
            value = template
            for key, path in paths.items():
                value = value.replace("{{" + key + "}}", path)
            if "{{" in value:
                raise ValueError("measurement deck has unresolved input references")
            return value
        execution = run_spectre_deck(
            workspace, render_deck=render, inputs=staged, output_names=outputs,
            timeout=action.timeout_seconds, resources=context.runtime,
            environment_values=context.runtime.environment,
        )
        # Persist every raw output before owner parsing, normalization or evaluation.
        for name, data in execution.raw_outputs.items():
            relative = Path(name)
            if len(relative.parts) > 1:
                workspace.directory("outputs", *relative.parts[:-1])
            artifacts.append(Artifact("spectre", "raw.cadence-spectre",
                                      workspace.write_bytes("outputs", relative.parts, data)))
        evaluation = _invoke(context, action, {
            **request, "phase": "evaluate", "condition": condition,
            "raw_outputs": {name: data.decode("utf-8") for name, data in execution.raw_outputs.items()},
        })
        if set(evaluation) != {"measurements", "normalized_csv"}:
            raise ValueError("measurement evaluation requires measurements and normalized_csv")
        measurement = evaluation["measurements"]
        if not isinstance(measurement, dict) or type(measurement.get("passed")) is not bool:
            raise ValueError("measurement result requires a boolean passed field")
        evidence = {
            "schema": 1, "contract_kind": "measurement-evidence",
            "owner": context.owner, "plan_identity": context.plan_identity,
            "run_id": context.run_id, "step": context.step.id,
            "condition": condition, "measurements": measurement,
            "product_qualification_conclusion": False,
        }
        artifacts.append(Artifact("measurement", "evidence.measurement", context.write_text(
            "measurement", "measurements.json", json.dumps(evidence, allow_nan=False, indent=2) + "\n",
        )))
        normalized = evaluation["normalized_csv"]
        if not isinstance(normalized, str):
            raise ValueError("normalized measurement must be CSV text")
        artifacts.append(Artifact("measurement", "table.measurement", context.write_text(
            "measurement", "waveforms.csv", normalized,
        )))
        return StepResult("succeeded" if measurement["passed"] else "failed", tuple(artifacts),
                          message="" if measurement["passed"] else "owner measurement contract failed")
    except (RuntimeError, ValueError) as exc:
        if process_group_cleanup_uncertainty(exc) is not None:
            raise
        artifacts.append(Artifact("log", "log.measurement", context.write_text("log", "measurement-error.log", str(exc))))
        return StepResult("failed", tuple(artifacts), message=str(exc))
