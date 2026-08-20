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


def parse_kv_pairs(items: list[str], ctx: str = "NAME=VAL") -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            die(f"参数格式错误：期望 {ctx}，得到 {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            die(f"参数名为空：{item!r}")
        values[key] = value.strip()
    return values
