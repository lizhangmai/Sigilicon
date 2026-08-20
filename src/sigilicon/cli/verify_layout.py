"""Internal XStream/Calibre adapter used by ``flow layout verify``."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sigilicon.cli.common import die
from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.layout_verification import execute_layout_verification_spec


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--check", choices=("drc", "lvs", "all"), default="all")
    parser.add_argument("--xstream-timeout", type=int, default=120)
    parser.add_argument("--calibre-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    root = discover_project_context(__file__).project_root
    checks = ("drc", "lvs") if args.check == "all" else (args.check,)
    try:
        client = client_factory()
        results = []
        for check in checks:
            spec, result = execute_layout_verification_spec(
                args.spec,
                root,
                client,
                check=check,
                xstream_timeout=args.xstream_timeout,
                calibre_timeout=args.calibre_timeout,
            )
            results.append((spec, result))
    except (OSError, RuntimeError, ValueError) as exc:
        die(f"ERROR: {exc}")
    for spec, result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{result.check}] {status} {spec.library}/{spec.cell}/{spec.view}")
        print(f"[fingerprint] {result.layout_fingerprint}")
        print(f"[details] {dict(result.details)}")
        print(f"[artifact] {result.manifest_path}")
    return 0 if all(result.passed for _spec, result in results) else 2
