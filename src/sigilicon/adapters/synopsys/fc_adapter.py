"""Fusion Compiler physical-implementation adapter."""

from __future__ import annotations

from sigilicon.execution.artifact_reference import ArtifactReference

from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution._values import ExecutionError
from sigilicon.execution._io import ExecutionIO
from pathlib import Path
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty
from sigilicon.adapters.synopsys._common import (
    _ToolVerdict,
    _archive_directory,
    _logs,
    _run_script,
    _runtime_environment,
    _safe_relative,
)

from sigilicon.adapters.synopsys.planning import FcAction, RunnerAdapter, require_action

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
            mapped_netlist = context.artifacts(ArtifactReference(synthesis, "mapped-netlist", "netlist.verilog"))[0]
            mapped_constraints = context.artifacts(ArtifactReference(synthesis, "mapped-constraints", "constraints.sdc"))[0]
            reference_root = context.artifact_directory(
                ArtifactReference(reference, "reference-library", "library.synopsys-ndm", "many"),
                action.reference_library,
            )
            output_names = {output.role: output.path for output in action.outputs}
            required = frozenset(output_names)
            role_environment = {output.role: output.environment for output in action.outputs}
            output_kinds = {output.role: output.kind for output in action.outputs}
            environment.update(
                {
                    "SIGILICON_FC_MAPPED_NETLIST": str(mapped_netlist.path),
                    "SIGILICON_FC_MAPPED_SDC": str(mapped_constraints.path),
                    "SIGILICON_FC_REFERENCE_NDM": str(reference_root),
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
                for role in output_names:
                    environment_name = role_environment[role]
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
            # Preserve generated evidence even when the runner or evaluator failed.
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
                report = scratch.path / "library-check-report" / "check_workspace.rpt"
                artifacts = reference_artifacts
                if report.is_file() and not report.is_symlink():
                    artifacts += (context.copy_output(
                        role="library-check-report",
                        kind="report.synopsys",
                        source=report,
                        filename="check_workspace.rpt",
                    ),)
                required = frozenset({"reference-library", "library-check-report"})
            else:
                copied: list[Artifact] = []
                for role in sorted(required, key=lambda role: (role == "checkpoint", role)):
                    role_root = scratch.path / role
                    if role == "checkpoint":
                        checkpoint = role_root / output_names[role]
                        if not checkpoint.is_dir() or checkpoint.is_symlink():
                            continue
                        archive = scratch.path / (Path(output_names[role]).name + ".tar")
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
                            kind=output_kinds[role],
                            source=path,
                            filename=path.relative_to(role_root).as_posix(),
                        )
                        for path in paths
                        if path.is_file() and not path.is_symlink()
                    )
                artifacts = tuple(copied)
            published = (*logs, *artifacts)
            if completed.returncode:
                return StepResult(
                    "failed", published, message=f"FC runner exited {completed.returncode}"
                )
            if {artifact.role for artifact in artifacts} != required:
                return StepResult("failed", published, message="FC omitted one or more result roles")
            if target == "pnr":
                verdict_path = (
                    scratch.path
                    / "execution-verdict"
                    / output_names["execution-verdict"]
                )
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
