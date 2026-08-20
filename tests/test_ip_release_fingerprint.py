from __future__ import annotations

from pathlib import Path

from sigilicon.domain.ip_release import (
    ReleaseFingerprintSource,
    release_source_fingerprint,
)


ATTRIBUTES = {
    "ip_name": "fixture-ip",
    "oa": {"library": "fixture", "top_cell": "TOP"},
}


def _fingerprint(
    root: Path, sources: tuple[tuple[str, Path], ...]
) -> str:
    return release_source_fingerprint(
        attributes=ATTRIBUTES,
        sources=(
            ReleaseFingerprintSource(logical_role=role, source=source)
            for role, source in sources
        ),
        project_root=root,
    )


def test_source_relocation_does_not_change_release_fingerprint(
    tmp_path: Path,
) -> None:
    original_model = tmp_path / "original/model.py"
    relocated_model = tmp_path / "relocated/model.py"
    original_contract = tmp_path / "contracts/original.toml"
    relocated_contract = tmp_path / "contracts/relocated.toml"
    for path in (
        original_model,
        relocated_model,
        original_contract,
        relocated_contract,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    original_model.write_text("VALUE = 1\n", encoding="utf-8")
    relocated_model.write_text("VALUE = 1\n", encoding="utf-8")
    original_contract.write_text(
        'algorithm_model = "../original/model.py"\n', encoding="utf-8"
    )
    relocated_contract.write_text(
        'algorithm_model = "../relocated/model.py"\n', encoding="utf-8"
    )

    original = _fingerprint(
        tmp_path,
        (
            ("algorithm-model", original_model),
            ("controller-contract", original_contract),
        ),
    )
    relocated = _fingerprint(
        tmp_path,
        (
            ("algorithm-model", relocated_model),
            ("controller-contract", relocated_contract),
        ),
    )

    assert relocated == original


def test_source_content_change_changes_release_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "model.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = _fingerprint(tmp_path, (("algorithm-model", source),))

    source.write_text("VALUE = 2\n", encoding="utf-8")
    after = _fingerprint(tmp_path, (("algorithm-model", source),))

    assert after != before


def test_source_role_change_changes_release_fingerprint(tmp_path: Path) -> None:
    generator = tmp_path / "generator.py"
    integrator = tmp_path / "integrator.py"
    generator.write_text("KIND = 'generator'\n", encoding="utf-8")
    integrator.write_text("KIND = 'integrator'\n", encoding="utf-8")

    expected = _fingerprint(
        tmp_path,
        (("generator-model", generator), ("integrator-model", integrator)),
    )
    swapped = _fingerprint(
        tmp_path,
        (("generator-model", integrator), ("integrator-model", generator)),
    )

    assert swapped != expected
