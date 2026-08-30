"""Internal XStream/Calibre adapter used by ``sigilicon layout verify``."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import discover_project_contract
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.layout_verification import execute_layout_verification_set
from sigilicon.workflows.project import load_project


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
    project: Any | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--check", choices=("drc", "lvs", "all"), default="all")
    parser.add_argument("--xstream-timeout", type=int, default=120)
    parser.add_argument("--calibre-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    canonical_project = (
        project
        if project is not None
        else load_project(discover_project_contract(__file__))
    )
    checks = ("drc", "lvs") if args.check == "all" else (args.check,)
    try:
        client = client_factory()
        results = execute_layout_verification_set(
            args.spec,
            client,
            project=canonical_project,
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
