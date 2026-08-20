"""Optional legacy ADE/Xcelium spec entrypoints.

The active OA flow uses ``simulation.toml`` and ``setup.il`` through the OA
workflows.  These wrappers remain only for the older declarative AMS/ADE
interface and are intentionally kept outside the current spec entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sigilicon.ams.spec import AmsSpec, load_ams_spec
from sigilicon.workflows.ams_ade_run import AdeRunResult, run_ade
from sigilicon.workflows.ams_ade_setup import AdeSetupResult, setup_ade


@dataclass(frozen=True)
class AmsExecution:
    spec: AmsSpec
    result: AdeSetupResult | AdeRunResult


def execute_ade_setup_spec(
    spec_path: Path,
    project_root: Path,
    client: Any,
    *,
    overwrite: bool,
    quarantine_stale_locks: bool,
) -> AmsExecution:
    spec = load_ams_spec(spec_path, project_root=project_root)
    return AmsExecution(
        spec,
        setup_ade(
            spec,
            client,
            overwrite=overwrite,
            quarantine_stale_locks=quarantine_stale_locks,
        ),
    )


def execute_ade_run_spec(
    spec_path: Path,
    project_root: Path,
    client: Any,
    *,
    variables: Mapping[str, str],
    timeout: int,
) -> AmsExecution:
    spec = load_ams_spec(spec_path, project_root=project_root)
    return AmsExecution(
        spec,
        run_ade(
            spec,
            client,
            variables=variables,
            timeout=timeout,
        ),
    )
