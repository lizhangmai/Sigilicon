"""使用 PDK 声明的原始 CDF 参数名修改器件参数并触发回调。

用法::

    This module is an internal guarded adapter used by source-driven flows;
    it is intentionally not exposed as a Pixi task.

注意:
  - 参数名不会做 PDK 专用别名映射，必须与目标器件 CDF 完全一致。
  - 目标 cell 必须属于当前项目且没有任何可见/隐藏 open view 或 OA lock。
  - 更新在单个 unwindProtect 中完成 CDF 恢复、dbSave 和 dbClose。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from sigilicon.paths import ProjectContext, discover_project_context
from sigilicon.cli.common import add_json_arg, die, emit_json, parse_kv_pairs
from sigilicon.virtuoso.client import get_client
from sigilicon.workflows.virtuoso_operations import update_instance_parameters


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] = get_client,
) -> int:
    p = argparse.ArgumentParser(description="改器件 CDF 参数")
    p.add_argument("lib")
    p.add_argument("cell")
    p.add_argument("inst", help="实例名，如 M0 / V0")
    p.add_argument(
        "params", nargs="+", metavar="NAME=VAL", help="参数，如 width=500n"
    )
    add_json_arg(p)
    args = p.parse_args(argv)

    kv = parse_kv_pairs(args.params, ctx="NAME=VAL")
    if not kv:
        die("至少需要一个 NAME=VAL 参数")

    try:
        paths = discover_project_context(__file__)
        result = update_instance_parameters(
            client_factory(),
            paths,
            args.lib,
            args.cell,
            args.inst,
            kv,
        )
    except Exception as exc:  # noqa: BLE001
        die(f"设置参数失败：{exc}")

    if args.json:
        emit_json(
            {
                "instance": args.inst,
                "cell": f"{args.lib}/{args.cell}",
                "applied": result.applied,
                "before": result.before,
                "after": result.after,
            }
        )
        return 0

    print(f"{args.inst}  ({args.lib}/{args.cell})")
    print(f"  applied: {', '.join(f'{k}={v}' for k, v in result.applied.items())}")
    print("  changed:")
    keys = set(result.applied)
    for key in sorted(keys):
        b = result.before.get(key, "?")
        a = result.after.get(key, "?")
        mark = " *" if str(b) != str(a) else ""
        print(f"    {key}: {b} -> {a}{mark}")
    return 0
