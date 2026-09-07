from __future__ import annotations

from types import MappingProxyType

import pytest

from sigilicon.canonical import canonical_json


def test_canonical_json_accepts_frozen_mappings() -> None:
    frozen = MappingProxyType({"name": "fixture", "value": 1})

    assert canonical_json(frozen) == canonical_json(dict(frozen))


@pytest.mark.parametrize(
    "value",
    [{1: "one"}, {"value": float("nan")}, {"value": float("inf")}],
)
def test_canonical_json_rejects_ambiguous_values(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_json(value)
