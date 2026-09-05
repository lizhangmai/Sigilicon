"""Fusion Compiler physical-implementation adapter."""

from __future__ import annotations

from sigilicon.execution._model import (
    Artifact,
    ExecutionError,
    ExecutionIO,
    StepResult,
)
from pathlib import (
    Path,
)
from sigilicon.external_tools import (
    owned_scratch_directory,
    process_group_cleanup_uncertainty,
)
from sigilicon.adapters.synopsys._common import (
    _ToolVerdict,
    _archive_directory,
    _artifact,
    _logs,
    _run_script,
    _runtime_environment,
    _safe_relative,
)

from sigilicon.adapters.synopsys.planning import FcAction, RunnerAdapter, require_action, FC_OUTPUT_ENVIRONMENT

class FcAdapter(RunnerAdapter):
    name = "synopsys.fc"
    action_type = FcAction

    def run(self, context: ExecutionIO) -> StepResult:
        step = context.step
        action = require_action(step, FcAction)
        target = action.target
        runtime = _runtime_environment(context.runtime, context.step)
        environment = runtime.values
        environment.update(
            {
                "SIGILICON_DESIGN_VARIANT": action.invocation.variant,
                "SIGILICON_DESIGN_CORNER": action.corner,
                "SIGILICON_DESIGN_TOP": action.top,
            }
        )
        held_files = list(runtime.files)
        held_directories = list(runtime.directories)
        reference_name: str | None = None
        output_names: dict[str, str] = {}
        required: frozenset[str] = frozenset()
        if target == "library":
            reference_name = _safe_relative(
                action.reference_library,
                "reference library output",
            )
        else:
            synthesis = action.synthesis_step
            reference = action.reference_step
            mapped_netlist = _artifact(context, synthesis, "mapped-netlist")
            mapped_constraints = _artifact(context, synthesis, "mapped-constraints")
            reference_files = context.artifacts(reference, "reference-library")
            if not reference_files:
                raise ExecutionError("reference-library step published no files")
            reference_name = action.reference_library
            candidates = {
                parent
                for artifact in reference_files
                for parent in artifact.path.parents
                if parent.name == reference_name
            }
            if len(candidates) != 1:
                raise ExecutionError("cannot reconstruct the reference-library directory")
            output_names = {output.role: output.path for output in action.outputs}
            required = frozenset(output_names)
            role_environment = FC_OUTPUT_ENVIRONMENT
            environment.update(
                {
                    "SIGILICON_FC_MAPPED_NETLIST": str(mapped_netlist.path),
                    "SIGILICON_FC_MAPPED_SDC": str(mapped_constraints.path),
                    "SIGILICON_FC_REFERENCE_NDM": str(next(iter(candidates))),
                    "SIGILICON_IMPLEMENTATION_EVALUATOR": str(
                        context.source_path(action.evaluator)
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
                invocation=action.invocation,
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
                published = (*logs, *artifacts)
                try:
                    verdict = _ToolVerdict.load(
                        verdict_path,
                        context=context,
                        stage="physical-implementation",
                        variant=action.invocation.variant,
                        corner=action.corner,
                    )
                except ExecutionError as exc:
                    return StepResult("failed", published, message=str(exc))
                if not verdict.passed:
                    return StepResult(
                        "failed",
                        published,
                        message="FC execution completed but owner evidence failed",
                    )
                return StepResult.succeeded(artifacts=published)
            return StepResult.succeeded(artifacts=(*logs, *artifacts))
