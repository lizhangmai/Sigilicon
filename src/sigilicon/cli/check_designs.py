"""Check the active project's source and integration contracts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from sigilicon.cli.common import die, emit_json
from sigilicon.paths import discover_project_contract
from sigilicon.workflows.repository_checks import check_project_designs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        report = check_project_designs(discover_project_contract(__file__))
    except (OSError, RuntimeError, ValueError) as error:
        die(f"ERROR: {error}")
    emit_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
