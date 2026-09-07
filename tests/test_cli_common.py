from __future__ import annotations

import pytest

from sigilicon.cli.common import emit_json


def test_emit_json_rejects_non_portable_values() -> None:
    with pytest.raises(TypeError, match="cannot canonically serialize"):
        emit_json({"value": object()})
