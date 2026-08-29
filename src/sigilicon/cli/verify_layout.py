"""Internal XStream/Calibre adapter used by ``flow layout verify``."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import discover_project_contract
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.project_layout import ProjectLayoutWorkflow


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
    workflow: ProjectLayoutWorkflow | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--check", choices=("drc", "lvs", "all"), default="all")
    parser.add_argument("--xstream-timeout", type=int, default=120)
    parser.add_argument("--calibre-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    project_layout = (
        workflow
        if workflow is not None
        else ProjectLayoutWorkflow.from_file(discover_project_contract(__file__))
    )
    checks = ("drc", "lvs") if args.check == "all" else (args.check,)
    try:
        client = client_factory()
        results = project_layout.verify(
            args.spec,
            client,
            checks=checks,
            xstream_timeout=args.xstream_timeout,
            calibre_timeout=args.calibre_timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    for spec, result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{result.check}] {status} {spec.library}/{spec.cell}/{spec.view}")
        print(f"[details] {dict(result.details)}")
        print(f"[artifact] {result.manifest_path}")
    return 0 if all(result.passed for _spec, result in results) else 2
