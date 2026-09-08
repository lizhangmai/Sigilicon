"""Library Compiler plugin: design Tcl in, one typed DB artifact out."""
from dataclasses import asdict, dataclass
import re

from sigilicon.adapters.synopsys._common import _logs, _positive_integer, _runtime_environment, _strict_config, _text
from sigilicon.adapters.synopsys.planning import source
from sigilicon.adapters.synopsys.tcl import completion, run_tcl, verdict
from sigilicon.execution.adapter import AdapterPreparation
from sigilicon.execution.artifact_reference import ArtifactProduct, StepContract
from sigilicon.execution._result import StepResult
from sigilicon.execution._values import ContractError
from sigilicon.execution.runtime import preflight_environment
from sigilicon.external_tools import owned_scratch_directory, process_group_cleanup_uncertainty

TOOL = "SIGILICON_SYNOPSYS_LC_SHELL"


@dataclass(frozen=True)
class LcAction:
    script: str
    liberty: str
    library: str
    success_marker: str
    timeout_seconds: int

    @classmethod
    def compile(cls, step):
        config = _strict_config(step, frozenset({"script", "liberty", "library", "success_marker", "timeout_seconds"}))
        library = _text(config, "library")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", library):
            raise ContractError("Library Compiler requires a library identifier")
        if TOOL not in step.runtime.tools:
            raise ContractError(f"Library Compiler runtime must bind {TOOL}")
        return cls(source(step, "script"), source(step, "liberty"), library,
                   _text(config, "success_marker"), _positive_integer(config, "timeout_seconds"))

    @property
    def record(self):
        return {"kind": "synopsys.library-compiler", **asdict(self)}

    @property
    def contract(self):
        return StepContract(produces=(
            ArtifactProduct("log", "log.synopsys", "many"),
            ArtifactProduct("compiled-library", "library.synopsys-db", path="library.db"),
            ArtifactProduct("execution-verdict", "evidence.tool-verdict", path="verdict.json")))


class LibraryCompilerAdapter:
    name = "synopsys.library-compiler"

    def contract(self, project, step):
        return LcAction.compile(step).contract

    def prepare(self, project, step, resources):
        return AdapterPreparation(action=LcAction.compile(step))

    def preflight(self, step, resources):
        return preflight_environment(step.runtime, resources)

    def run(self, context):
        context.step.validate_action()
        action = context.step.action
        if not isinstance(action, LcAction):
            raise ContractError("Library Compiler requires a compiled action")
        runtime = _runtime_environment(context.runtime, context.step)
        with owned_scratch_directory(prefix=f"sigilicon-lc-{context.run_id}-",
                retain_on_error=lambda exc: process_group_cleanup_uncertainty(exc) is not None) as scratch:
            db = scratch.path / "library.db"
            completed = run_tcl(context, runtime, tool=TOOL, script=action.script,
                output_root=scratch.path, timeout_seconds=action.timeout_seconds,
                environment={"SIGILICON_LC_LIBERTY": str(context.source_path(action.liberty)),
                             "SIGILICON_LC_LIBRARY": action.library,
                             "SIGILICON_LC_DB": str(db)})
            artifacts = list(_logs(context, completed.stdout, completed.stderr or ""))
            checks = completion(completed, action.success_marker)
            checks["compiled_library"] = db.is_file() and not db.is_symlink() and db.stat().st_size > 0
            if checks["compiled_library"]:
                artifacts.append(context.copy_output(role="compiled-library", kind="library.synopsys-db",
                                                     source=db, filename="library.db"))
            artifacts.append(verdict(context, stage="library-compilation", variant=action.library,
                                     corner="", checks=checks))
            return StepResult("succeeded" if all(checks.values()) else "failed", tuple(artifacts),
                              message="" if all(checks.values()) else "Library Compiler did not satisfy its execution contract")
