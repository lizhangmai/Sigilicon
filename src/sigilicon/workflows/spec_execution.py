"""Active standalone spec entrypoint used by characterization workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.ams.spec import AmsSpec, load_ams_spec
from sigilicon.workflows.ams_standalone import StandaloneResult, run_standalone
from sigilicon.workflows.design_lifecycle import attest_design_set


@dataclass(frozen=True)
class AmsExecution:
    spec: AmsSpec
    result: StandaloneResult


def execute_standalone_spec(
    spec_path: Path,
    project_root: Path,
    *,
    xrun: Path | None,
    timeout: int,
    oa_client: Any,
) -> AmsExecution:
    spec = load_ams_spec(spec_path, project_root=project_root)
    attest_design_set(
        (spec.design.path,),
        oa_client,
        project_root=project_root,
    )
    return AmsExecution(
        spec,
        run_standalone(spec, xrun=xrun, timeout=timeout),
    )
