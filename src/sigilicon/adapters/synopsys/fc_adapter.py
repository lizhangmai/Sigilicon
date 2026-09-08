"""Managed FC-RM execution: one kernel step, one process, typed artifacts."""
from __future__ import annotations

import io
import hashlib
import gzip
import json
from pathlib import Path
import re
import shutil
import tarfile

from sigilicon.adapters.synopsys._common import _archive_directory, _logs, _run_script, _runtime_environment
from sigilicon.adapters.synopsys.planning import Invocation
from sigilicon.adapters.synopsys.fc_flow import FcAction, LABELS, METHOD, compile_steps
from sigilicon.adapters.synopsys.fc_rm import materialize
from sigilicon.adapters.synopsys.fc_reports import observations
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactReference
from sigilicon.execution._plan import PreflightCheck
from sigilicon.execution._result import Artifact, StepResult
from sigilicon.execution._values import ExecutionError
from sigilicon.execution.runtime import preflight_environment
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty


def restore_checkpoint(payload: bytes, destination: Path) -> None:
    """Reject links, traversal and unrelated roots before extracting any data."""
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        members = archive.getmembers()
        if not members:
            raise ExecutionError("empty FC checkpoint")
        for member in members:
            path = Path(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                or path.parts[0] != "design.dlib"
                or not (member.isfile() or member.isdir())):
                raise ExecutionError("unsafe FC checkpoint archive member")
        archive.extractall(destination, members=members, filter="data")
    if not (destination / "design.dlib/lib.ndm").is_file():
        raise ExecutionError("FC checkpoint is missing its design library index")


class FcAdapter:
    name = "synopsys.fc"

    def compile_steps(self, project, step):
        return compile_steps(step)

    def contract(self, project, step):
        return FcAction.compile(step).contract

    def prepare(self, project, step, resources):
        return AdapterPreparation(action=FcAction.compile(step))

    def preflight(self, step, resources):
        return (PreflightCheck("fc-methodology", METHOD, "ready",
                "local RM; flat RTL-to-GDS; no DFT or signoff"),
                *preflight_environment(step.runtime, resources))

    def run(self, context):
        action = context.step.action
        if not isinstance(action, FcAction):
            raise ExecutionError("FC requires a compiled methodology action")
        context.step.validate_action()
        runtime = _runtime_environment(context.runtime, context.step)
        with owned_scratch_directory(prefix=f"sigilicon-fc-{context.run_id}-",
                retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc) is not None) as scratch:
            root = scratch.path
            reference_identity = []
            if action.stage != "reference-library":
                reference = ArtifactReference("reference-library", "reference-library", "library.synopsys-ndm", "many")
                source = context.artifact_directory(reference, "references")
                shutil.copytree(source, root / "references")
                reference_identity = sorted(
                    ({"path": item.path.relative_to(source).as_posix(), "sha256": item.sha256}
                     for item in context.artifacts(reference)),
                    key=lambda item: item["path"])
            if action.previous not in (None, "reference-library"):
                checkpoint = context.artifacts(ArtifactReference(action.previous, "checkpoint", "checkpoint.synopsys-dlib-tar"))[0]
                metadata = json.loads(context.artifacts(ArtifactReference(
                    action.previous, "checkpoint-metadata", "evidence.fc-checkpoint"))[0].read_bytes())
                if (metadata.get("methodology") != METHOD
                    or metadata.get("block") != action.hdl.top + "/" + LABELS[action.previous]
                    or metadata.get("references") != reference_identity
                    or metadata.get("checkpoint_sha256") != checkpoint.sha256):
                    raise ExecutionError("FC checkpoint metadata/dependency identity mismatch")
                restore_checkpoint(checkpoint.read_bytes(), root)
            runner = materialize(root, action, context)
            managed_runner = context.workspace("synopsys", {}, tool_work_root=context.work_directory).write_text(
                "work", ("fc-run.sh",), runner.read_text())
            environment = dict(runtime.values)
            environment.update({
                "SIGILICON_FC_LAUNCH": str(root / "generated/launch.tcl"),
                "SIGILICON_FC_LOG": str(root / "logs_fc" / (LABELS[action.stage] + ".log")),
            })
            completed = _run_script(context, environment, argument="",
                invocation=Invocation("", "", action.timeout_seconds),
                held_executables=runtime.tools, held_files=runtime.files,
                held_directories=runtime.directories, generated_runner=managed_runner)
            published = list(_logs(context, completed.stdout, completed.stderr or ""))
            def collect(directory, role, kind, prefix=""):
                if not directory.is_dir():
                    return []
                result = []
                for path in sorted(directory.rglob("*")):
                    if role == "reference-library" and any(
                            part.startswith("@@") for part in path.relative_to(directory).parts):
                        continue  # LC backup libraries are not active dependencies.
                    if path.is_file() and not path.is_symlink():
                        result.append(context.copy_output(role=role, kind=kind, source=path,
                            filename=prefix + path.relative_to(directory).as_posix()))
                published.extend(result)
                return result
            collect(root / "logs_fc", "log", "log.synopsys", "logs/fc/")
            collect(root / "reports", "report", "report.synopsys", "reports/")
            collect(root / "reports_fc", "report", "report.synopsys", "reports/")
            log = completed.stdout + "\n" + (completed.stderr or "")
            log_file = root / "logs_fc" / (LABELS[action.stage] + ".log")
            if log_file.is_file():
                log += "\n" + log_file.read_text(errors="replace")
            errors = sorted(set(re.findall(r"^(?:Error:|RM-error[^\n]*:)[^\n]*", log, re.M)))
            marker = root / "stage_complete.rpt"
            expected = f"stage={action.stage}\nblock={action.hdl.top}/{LABELS[action.stage]}\n"
            checks = {
                "process_exit": completed.returncode == 0,
                "tool_errors": not errors,
                "completed_block": marker.is_file() and marker.read_text() == expected,
                "rm_completion": (root / LABELS[action.stage]).is_file(),
            }
            if action.stage == "reference-library":
                outputs = []
                for library in sorted((root / "references").glob("*")):
                    if (library.name.startswith("@@") or library.is_symlink()
                            or not (library / "registry.dat").is_file()):
                        continue
                    outputs.extend(collect(library, "reference-library", "library.synopsys-ndm",
                                           "references/" + library.name + "/"))
                checks["reference_library"] = bool(outputs)
            else:
                library = root / "design.dlib"
                checks["checkpoint"] = (library / "lib.ndm").is_file()
                if checks["checkpoint"]:
                    archive = root / "design.dlib.tar"
                    _archive_directory(library, archive, "design.dlib")
                    artifact = context.copy_output(role="checkpoint", kind="checkpoint.synopsys-dlib-tar",
                        source=archive, filename=archive.name)
                    published.append(artifact)
                    metadata = {"schema": 1, "methodology": METHOD,
                        "block": action.hdl.top + "/" + LABELS[action.stage],
                        "references": reference_identity,
                        "checkpoint_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "run_id": context.run_id, "step_id": context.step.id,
                        "plan_identity": context.plan_identity}
                    path = context.write_text("checkpoint-metadata", "checkpoint.json", json.dumps(metadata) + "\n")
                    published.append(Artifact("checkpoint-metadata", "evidence.fc-checkpoint", path))
                if action.stage == "export":
                    exports = collect(root / "outputs_fc", "implementation", "implementation.fc-export", "implementation/")
                    export_root = root / "exports"
                    export_root.mkdir()
                    def export_file(basename, role, kind, name):
                        source = root / "outputs_fc" / basename
                        compressed = source.with_name(source.name + ".gz")
                        try:
                            data = gzip.decompress(compressed.read_bytes()) if compressed.is_file() else source.read_bytes()
                            if not data:
                                return False
                        except (OSError, EOFError):
                            return False
                        output = export_root / name
                        output.write_bytes(data)
                        published.append(context.copy_output(role=role, kind=kind, source=output, filename=name))
                        return True
                    for suffix, role, kind in (
                        ("v", "routed-netlist", "netlist.verilog"),
                        ("pt.v", "timing-netlist", "netlist.verilog"),
                        ("lvs.v", "lvs-netlist", "netlist.verilog"),
                        ("gds", "layout-stream", "layout.gds"),
                        ("def", "routed-def", "layout.def"),
                    ):
                        checks["export_" + role] = export_file(
                            "write_data." + suffix, role, kind, "design." + suffix)
                    spefs = sorted({p.path.name.removesuffix(".gz") for p in exports
                        if p.path.name.endswith((".spef", ".spef.gz"))})
                    checks["export_parasitics"] = bool(spefs) and all(
                        export_file(name, "parasitics", "parasitics.spef", name) for name in spefs)
            verdict = {"schema": 1, "methodology": METHOD, "owner": context.owner,
                "stage": action.stage, "run_id": context.run_id,
                "plan_identity": context.plan_identity, "passed": all(checks.values()),
                "checks": checks, "errors": errors,
                "product_qualification_conclusion": False, "signoff": "not_requested",
                "observations": observations(root / "reports_fc" / LABELS[action.stage])}
            path = context.write_text("execution-verdict", "verdict.json", json.dumps(verdict, indent=2) + "\n")
            published.append(Artifact("execution-verdict", "evidence.tool-verdict", path))
            return StepResult("succeeded" if all(checks.values()) else "failed", tuple(published),
                message="" if all(checks.values()) else "FC stage did not satisfy its execution contract")
