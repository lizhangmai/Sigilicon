"""Read-only library and cell discovery through escaped SKILL."""

from __future__ import annotations

from typing import Any

from sigilicon.virtuoso.bridge import decode_skill_output

from sigilicon.virtuoso.oa import skill_quote


def list_libraries(client: Any) -> dict[str, list[str]]:
    return {"libraries": client.library.list(timeout=20)}


def list_cells(client: Any, library: str) -> dict[str, Any]:
    quoted = skill_quote(library)
    result = client.execute_skill(f"ddGetObj({quoted})~>name", timeout=10)
    if result.errors:
        raise RuntimeError(result.errors[0])
    if not (result.output or "").strip().strip('"'):
        raise RuntimeError(
            f"找不到库 {library!r}（确认 cds.lib 里 DEFINE 了它）"
        )
    result = client.execute_skill(
        f'''let((out views)
  out = ""
  foreach(cell ddGetObj({quoted})~>cells
    views = ""
    foreach(view cell~>views views = strcat(views view~>name " "))
    out = strcat(out sprintf(nil "%s|%s\\n" cell~>name views)))
  out)''',
        timeout=20,
    )
    if result.errors:
        raise RuntimeError(result.errors[0])
    cells = []
    for line in decode_skill_output(result.output or "").splitlines():
        if not line.strip():
            continue
        name, _, views = line.partition("|")
        cells.append({"name": name.strip(), "views": views.split()})
    cells.sort(key=lambda item: item["name"])
    return {"library": library, "cells": cells}
