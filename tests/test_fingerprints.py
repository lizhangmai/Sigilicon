from __future__ import annotations

from pathlib import Path

from sigilicon.domain.fingerprints import (
    semantic_file_fingerprint,
    source_fingerprint_set,
)


def _write(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def test_comments_and_formatting_change_exact_but_not_semantic_identity(
    tmp_path: Path,
) -> None:
    first = _write(
        tmp_path / "setup.il",
        'procedure(foo(lib cell)\n  ;; explanatory comment\n  hdbSetObjBindRule(cfg "schematic")\n)\n',
    )
    before = source_fingerprint_set({"setup/setup.il": first})
    first.write_text(
        'procedure( foo( lib cell ) ; moved comment\n'
        ' hdbSetObjBindRule( cfg "schematic" ) )\n',
        encoding="utf-8",
    )
    after = source_fingerprint_set({"setup/setup.il": first})

    assert before.exact != after.exact
    assert before.semantic == after.semantic


def test_circuit_content_change_changes_semantic_identity(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "testbench.scs",
        "subckt tb IN OUT\nR0 (OUT IN) resistor r=1k\nends tb\n",
    )
    before = semantic_file_fingerprint(source)
    source.write_text(
        "subckt tb IN OUT\nR0 (OUT IN) resistor r=2k\nends tb\n",
        encoding="utf-8",
    )
    assert before != semantic_file_fingerprint(source)


def test_setup_binding_change_changes_semantic_identity(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "setup.il",
        'hdbSetObjBindRule(cfg "dut" "schematic")\n',
    )
    before = semantic_file_fingerprint(source)
    source.write_text(
        'hdbSetObjBindRule(cfg "dut" "symbol")\n',
        encoding="utf-8",
    )
    assert before != semantic_file_fingerprint(source)


def test_calculator_expression_change_changes_semantic_identity(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "setup.il",
        'maeAddOutput("scalar" "tran" ?expr "value(VT(\\"/OUT\\") 1u)")\n',
    )
    before = semantic_file_fingerprint(source)
    source.write_text(
        'maeAddOutput("scalar" "tran" ?expr "value(VT(\\"/OUT\\") 2u)")\n',
        encoding="utf-8",
    )
    assert before != semantic_file_fingerprint(source)


def test_waveform_signal_change_changes_semantic_identity(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "native_rdb.toml",
        'schema = 2\n\n[[waveforms]]\nname = "wave"\nsignal = "/OUT"\n',
    )
    before = semantic_file_fingerprint(source)
    source.write_text(
        'schema = 2\n\n[[waveforms]]\nname = "wave"\nsignal = "/OTHER"\n',
        encoding="utf-8",
    )
    assert before != semantic_file_fingerprint(source)


def test_simulation_identity_change_is_semantic_and_exact(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "simulation.toml",
        '[testbench]\ncell = "tb_a"\nlibrary = "fixture_lib"\n',
    )
    before = source_fingerprint_set({"contract/simulation.toml": source})
    source.write_text(
        '# formatting/comment only\n\n[testbench]\n'
        'library = "fixture_lib"\ncell = "tb_b"\n',
        encoding="utf-8",
    )
    after = source_fingerprint_set({"contract/simulation.toml": source})

    assert before.exact != after.exact
    assert before.semantic != after.semantic


def test_simulation_comments_and_formatting_are_exact_only_changes(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "simulation.toml",
        '[testbench]\nlibrary = "fixture_lib"\ncell = "tb_a"\n',
    )
    before = source_fingerprint_set({"contract/simulation.toml": source})
    source.write_text(
        '# source ownership note\n\n[testbench]\ncell = "tb_a"\n'
        'library = "fixture_lib"\n',
        encoding="utf-8",
    )
    after = source_fingerprint_set({"contract/simulation.toml": source})

    assert before.exact != after.exact
    assert before.semantic == after.semantic
