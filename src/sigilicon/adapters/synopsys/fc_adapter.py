"""Fusion Compiler physical-implementation adapter."""

from __future__ import annotations

from sigilicon.adapters.synopsys._common import (
    Artifact, ContractError, ExecutionError, ExecutionIO, Path, PreflightCheck,
    Resources, Step, StepResult, _ToolVerdict, _archive_directory, _artifact,
    _base_checks, _declared_inputs, _logs, _mapping, _run_script,
    _runtime_environment, _safe_relative, _strict_config, _target, _text,
    owned_scratch_directory, preflight_environment,
    process_group_cleanup_uncertainty,
)

class FcAdapter:
    name = "synopsys.fc"
    prepare = _declared_inputs
    _fields = frozenset(
        {
            "corner",
            "evaluator",
            "outputs",
            "reference_library_output",
            "reference_step",
            "runner",
            "synthesis_step",
            "target",
            "timeout_seconds",
            "top",
            "variant",
        }
    )

    def preflight(self, step: Step, resources: Resources) -> tuple[PreflightCheck, ...]:
        _strict_config(step, self._fields)
        checks = _base_checks(step)
        target = _target(step.config)
        _text(step.config, "corner")
        if target not in {"library", "pnr"}:
            raise ContractError(f"unsupported FC target {target!r}")
        if target == "pnr":
            evaluator = _safe_relative(_text(step.config, "evaluator"), "evaluator")
            if evaluator not in step.sources:
                raise ContractError("FC evaluator must be inside the step source closure")
        checks.extend(preflight_environment(step.runtime, resources))
        return tuple(checks)

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        config = _strict_config(context.step, self._fields)
        target = _target(config)
        runtime = _runtime_environment(context.runtime, context.step)
        environment = runtime.values
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": _text(config, "variant"),
                "SIGILICON_DESIGN_CORNER": _text(config, "corner"),
                "SIGILICON_DESIGN_TOP": _text(config, "top"),
            }
        )
        held_files = list(runtime.files)
        held_directories = list(runtime.directories)
        reference_name: str | None = None
        output_names: dict[str, str] = {}
        required: frozenset[str] = frozenset()
        if target == "library":
            reference_name = _safe_relative(
                _text(config, "reference_library_output"),
                "reference library output",
            )
        else:
            synthesis = _text(config, "synthesis_step")
            reference = _text(config, "reference_step")
            mapped_netlist = _artifact(context, synthesis, "mapped-netlist")
            mapped_constraints = _artifact(context, synthesis, "mapped-constraints")
            reference_files = context.artifacts(reference, "reference-library")
            if not reference_files:
                raise ExecutionError("reference-library step published no files")
            reference_name = _text(config, "reference_library_output")
            candidates = {
                parent
                for artifact in reference_files
                for parent in artifact.path.parents
                if parent.name == reference_name
            }
            if len(candidates) != 1:
                raise ExecutionError("cannot reconstruct the reference-library directory")
            output_names = {
                key: _safe_relative(str(value), f"FC output {key}")
                for key, value in _mapping(config, "outputs").items()
                if isinstance(key, str) and isinstance(value, str)
            }
            required = frozenset(
                {
                    "routed-netlist",
                    "routed-constraints",
                    "layout-stream",
                    "checkpoint",
                    "design-check-report",
                    "structural-report",
                    "qor-report",
                    "timing-report",
                    "area-report",
                    "power-report",
                    "drc-report",
                    "physical-completion-report",
                    "tie-off-check-report",
                    "execution-verdict",
                }
            )
            if set(output_names) != required:
                raise ContractError("FC outputs do not match the physical result contract")
            role_environment = {
                "routed-netlist": "SIGILICON_FC_ROUTED_NETLIST",
                "routed-constraints": "SIGILICON_FC_ROUTED_CONSTRAINTS",
                "layout-stream": "SIGILICON_FC_GDS",
                "checkpoint": "SIGILICON_FC_CHECKPOINT",
                "design-check-report": "SIGILICON_FC_DESIGN_CHECK_REPORT",
                "structural-report": "SIGILICON_FC_STRUCTURAL_REPORT",
                "qor-report": "SIGILICON_FC_QOR_REPORT",
                "timing-report": "SIGILICON_FC_TIMING_REPORT",
                "area-report": "SIGILICON_FC_AREA_REPORT",
                "power-report": "SIGILICON_FC_POWER_REPORT",
                "drc-report": "SIGILICON_FC_DRC_REPORT",
                "physical-completion-report": "SIGILICON_FC_PHYSICAL_COMPLETION_REPORT",
                "tie-off-check-report": "SIGILICON_FC_TIE_OFF_CHECK_REPORT",
                "execution-verdict": "SIGILICON_FC_EXECUTION_VERDICT",
            }
            environment.update(
                {
                    "SIGILICON_FC_MAPPED_NETLIST": str(mapped_netlist.path),
                    "SIGILICON_FC_MAPPED_SDC": str(mapped_constraints.path),
                    "SIGILICON_FC_REFERENCE_NDM": str(next(iter(candidates))),
                    "SIGILICON_IMPLEMENTATION_EVALUATOR": str(
                        context.source_path(_text(config, "evaluator"))
                    ),
                }
            )
            held_files.extend(
                ("SIGILICON_FC_MAPPED_NETLIST", "SIGILICON_FC_MAPPED_SDC")
            )
            held_directories.append("SIGILICON_FC_REFERENCE_NDM")
        with owned_scratch_directory(
            prefix=f"sigilicon-fc-{context.run_id}-",
            retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc)
            is not None,
        ) as scratch:
            environment["SIGILICON_FC_WORK_ROOT"] = scratch.child_path
            if target == "library":
                assert reference_name is not None
                environment.update(
                    {
                        "SIGILICON_FC_REFERENCE_NDM": (
                            f"{scratch.child_path}/reference-library/"
                            f"{reference_name}"
                        ),
                        "SIGILICON_FC_LIBRARY_CHECK_REPORT": (
                            f"{scratch.child_path}/library-check-report/"
                            "check_workspace.rpt"
                        ),
                    }
                )
            else:
                for role, environment_name in role_environment.items():
                    environment[environment_name] = (
                        f"{scratch.child_path}/{role}/{output_names[role]}"
                    )
            completed = _run_script(
                context,
                environment,
                argument=target,
                held_executables=runtime.tools,
                held_files=tuple(held_files),
                held_directories=tuple(held_directories),
            )
            logs = _logs(context, completed.stdout, completed.stderr or "")
            if completed.returncode:
                return StepResult(
                    "failed", logs, message=f"FC runner exited {completed.returncode}"
                )
            if target == "library":
                assert reference_name is not None
                reference_root = (
                    scratch.path / "reference-library" / reference_name
                )
                reference_artifacts = tuple(
                    context.copy_output(
                        role="reference-library",
                        kind="library.synopsys-ndm",
                        source=path,
                        filename=(
                            Path(reference_name) / path.relative_to(reference_root)
                        ).as_posix(),
                    )
                    for path in sorted(reference_root.rglob("*"))
                    if path.is_file() and not path.is_symlink()
                )
                if not reference_artifacts:
                    raise ExecutionError(
                        "FC produced an empty reference-library directory"
                    )
                artifacts = (
                    *reference_artifacts,
                    context.copy_output(
                        role="library-check-report",
                        kind="report.synopsys",
                        source=(
                            scratch.path
                            / "library-check-report"
                            / "check_workspace.rpt"
                        ),
                        filename="check_workspace.rpt",
                    ),
                )
            else:
                copied: list[Artifact] = []
                for role in sorted(required):
                    role_root = scratch.path / role
                    if role == "checkpoint":
                        checkpoint = role_root / output_names[role]
                        archive = scratch.path / f"{output_names[role]}.tar"
                        _archive_directory(
                            checkpoint,
                            archive,
                            output_names[role],
                        )
                        copied.append(
                            context.copy_output(
                                role=role,
                                kind="checkpoint.synopsys-dlib-tar",
                                source=archive,
                                filename=archive.name,
                            )
                        )
                        continue
                    paths = (
                        role_root / output_names[role],
                    )
                    copied.extend(
                        context.copy_output(
                            role=role,
                            kind="result.synopsys-fc",
                            source=path,
                            filename=path.relative_to(role_root).as_posix(),
                        )
                        for path in paths
                        if path.is_file() and not path.is_symlink()
                    )
                artifacts = tuple(copied)
                if {artifact.role for artifact in artifacts} != required:
                    raise ExecutionError("FC omitted one or more physical result roles")
            if target == "pnr":
                verdict_path = (
                    scratch.path
                    / "execution-verdict"
                    / output_names["execution-verdict"]
                )
                verdict = _ToolVerdict.load(
                    verdict_path,
                    owner=context.owner,
                    stage="physical-implementation",
                    variant=_text(config, "variant"),
                )
                published = (*logs, *artifacts)
                if not verdict.passed:
                    return StepResult(
                        "failed",
                        published,
                        message="FC execution completed but owner evidence failed",
                    )
                return StepResult.succeeded(artifacts=published)
            return StepResult.succeeded(artifacts=(*logs, *artifacts))
