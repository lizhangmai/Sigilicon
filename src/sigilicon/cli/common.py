"""Presentation-only helpers shared by command-line entry points."""

from __future__ import annotations

import json
import sys
from typing import Any


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def add_json_arg(parser: Any) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON（便于管道 / 脚本处理）",
    )


def emit_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
