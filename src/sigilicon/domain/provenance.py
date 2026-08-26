"""Canonical JSON identity used only by immutable release packaging."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
