"""Presentation-only helpers shared by command-line entry points."""

from __future__ import annotations

import json
from typing import Any


def emit_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
