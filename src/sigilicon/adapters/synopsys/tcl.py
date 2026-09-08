"""Execute design-owned Tcl directly through the supervised tool interface.

No synthesis methodology is generated here. The design owns its Tcl source;
the adapter binds inputs, holds their identities and owns result publication.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import re

from sigilicon.execution._result import Artifact
from sigilicon.external_tools import (
    ProcessRequest, managed_process, owned_directory, owned_input_closure,
    owned_input_file,
)


def run_tcl(context, runtime, *, tool, script, output_root, environment,
            timeout_seconds, inputs=()):
    values = {**runtime.values, **environment,
              "SIGILICON_PLAN_IDENTITY": context.plan_identity,
              "SIGILICON_RUN_ID": context.run_id,
              "SIGILICON_STEP_ID": context.step.id}
    with ExitStack() as stack:
        sources = stack.enter_context(owned_directory(context.source_directory))
        closure = stack.enter_context(owned_input_closure(
            context.source_directory,
            files=tuple(context.source_path(path) for path in context.step.sources)))
        work = stack.enter_context(owned_directory(output_root))
        executable = stack.enter_context(context.runtime.owned_tool(context.step.runtime.tools[tool]))
        held = [sources, closure, work, executable]
        for name in runtime.files:
            file = stack.enter_context(owned_input_file(Path(values[name]), require_single_link=False))
            values[name] = file.child_named_path
            held.append(file)
        for name in runtime.directories:
            directory = stack.enter_context(owned_directory(Path(values[name])))
            values[name] = directory.child_path
            held.append(directory)
        # This also watches generated input manifests and producer artifacts.
        for path in dict.fromkeys(inputs):
            held.append(stack.enter_context(owned_input_file(path, require_single_link=True)))
        for name, value in tuple(values.items()):
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(context.source_directory):
                values[name] = f"{sources.child_path}/{path.relative_to(context.source_directory).as_posix()}"
        values["SIGILICON_OUTPUT_ROOT"] = work.child_path

        def visible():
            for item in held:
                item.require_visible()

        return managed_process.run(ProcessRequest(
            argv=(*executable.command, "-f", f"{sources.child_path}/{script}"),
            executable=executable.executable,
            cwd=Path(work.child_path), environment=values,
            timeout_seconds=timeout_seconds, before_spawn=visible))


def completion(completed, marker):
    log = completed.stdout + "\n" + (completed.stderr or "")
    return {
        "process_exit": completed.returncode == 0,
        "no_tool_errors": re.search(r"^\s*Error:", log, re.M) is None,
        "completed": log.splitlines().count(marker) == 1,
    }


def verdict(context, *, stage, variant, corner, checks):
    payload = dict(schema=2, contract_kind="tool-verdict", owner=context.owner,
                   stage=stage, variant=variant, corner=corner,
                   passed=all(checks.values()), product_qualification_conclusion=False,
                   checks=checks, plan_identity=context.plan_identity,
                   run_id=context.run_id, step_id=context.step.id)
    path = context.write_text("execution-verdict", "verdict.json", json.dumps(payload, indent=2) + "\n")
    return Artifact("execution-verdict", "evidence.tool-verdict", path)
