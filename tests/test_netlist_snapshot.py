from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.domain.netlist import (
    extract_subckt_body,
    iter_spectre_logical_lines,
    lower_subckt_default_parameters,
    load_netlist_snapshot,
    materialize_netlist_snapshot,
    parse_spectre_pwl_sources,
    render_canonical_cdl,
    resolve_netlist_hierarchy,
)


def _source(path: Path) -> Path:
    path.write_text("subckt cell A Y\nends cell\n", encoding="utf-8")
    return path


def test_snapshot_source_does_not_follow_a_symlink(tmp_path: Path) -> None:
    target = _source(tmp_path / "target.scs")
    alias = tmp_path / "alias.scs"
    alias.symlink_to(target)

    with pytest.raises(OSError):
        load_netlist_snapshot(alias)


def test_materialized_snapshot_does_not_follow_existing_symlink(
    tmp_path: Path,
) -> None:
    snapshot = load_netlist_snapshot(_source(tmp_path / "source.scs"))
    outside = tmp_path / "outside.scs"
    outside.write_text("do-not-touch\n", encoding="utf-8")
    destination = tmp_path / "run" / "source.scs"
    destination.parent.mkdir()
    destination.symlink_to(outside)

    with pytest.raises(OSError):
        materialize_netlist_snapshot(snapshot, destination)

    assert outside.read_text(encoding="utf-8") == "do-not-touch\n"


def test_materialized_snapshot_detects_inode_replacement(tmp_path: Path) -> None:
    snapshot = load_netlist_snapshot(_source(tmp_path / "source.scs"))
    artifact = materialize_netlist_snapshot(
        snapshot,
        tmp_path / "run" / "immutable.scs",
    )
    artifact.path.rename(artifact.path.with_suffix(".old"))
    artifact.path.write_text(snapshot.text, encoding="utf-8")

    with pytest.raises(RuntimeError, match="identity changed"):
        artifact.verify()


def test_materialized_snapshot_detects_parent_symlink_replacement(
    tmp_path: Path,
) -> None:
    snapshot = load_netlist_snapshot(_source(tmp_path / "source.scs"))
    artifact = materialize_netlist_snapshot(
        snapshot,
        tmp_path / "run" / "immutable.scs",
    )
    original_parent = tmp_path / "run-original"
    artifact.path.parent.rename(original_parent)
    artifact.path.parent.symlink_to(original_parent, target_is_directory=True)

    with pytest.raises(OSError):
        artifact.verify()


def test_lowering_subckt_defaults_is_mechanical_and_preserves_interface(
    tmp_path: Path,
) -> None:
    source = tmp_path / "parameterized.scs"
    source.write_text(
        "// canonical source remains parameterized\n"
        "subckt cell A Y VDD VSS parameters lch=30n wdev=120n\n"
        "M0 (Y A VSS VSS) nch_mac l=lch w=wdev nf=1\n"
        "ends cell\n",
        encoding="utf-8",
    )

    lowered = lower_subckt_default_parameters(load_netlist_snapshot(source), "cell")

    assert lowered.interfaces == {"cell": ("A", "Y", "VDD", "VSS")}
    assert "parameters" not in lowered.text
    assert "l=30n w=120n nf=1" in lowered.text
    assert "l=lch" not in lowered.text
    assert "w=wdev" not in lowered.text


def test_lowering_rejects_nonliteral_default_parameter(tmp_path: Path) -> None:
    source = tmp_path / "expression.scs"
    source.write_text(
        "subckt cell A Y parameters wdev=baseWidth\n"
        "M0 (Y A 0 0) nch_mac w=wdev\n"
        "ends cell\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-literal"):
        lower_subckt_default_parameters(load_netlist_snapshot(source), "cell")


def test_lowering_supports_spectre_body_parameter_statement(tmp_path: Path) -> None:
    source = tmp_path / "spectre-parameterized.scs"
    source.write_text(
        "subckt cell A Y VDD VSS\n"
        "parameters lch=30n wdev=100n nfdev=1\n"
        "M0 (Y A VSS VSS) nch_mac l=lch w=wdev nf=nfdev\n"
        "ends cell\n",
        encoding="utf-8",
    )

    lowered = lower_subckt_default_parameters(load_netlist_snapshot(source), "cell")

    assert lowered.interfaces == {"cell": ("A", "Y", "VDD", "VSS")}
    assert "parameters" not in lowered.text
    assert "l=30n w=100n nf=1" in lowered.text


def test_lowering_normalizes_a_continued_subckt_header_for_spicein(
    tmp_path: Path,
) -> None:
    source = tmp_path / "continued-header.scs"
    source.write_text(
        "subckt cell A B \\\n"
        "    Y VDD VSS\n"
        "X0 (A B Y VDD VSS) child\n"
        "ends cell\n",
        encoding="utf-8",
    )

    lowered = lower_subckt_default_parameters(load_netlist_snapshot(source), "cell")

    assert lowered.interfaces == {"cell": ("A", "B", "Y", "VDD", "VSS")}
    assert lowered.text.startswith("subckt cell A B Y VDD VSS\n")
    assert "\\\n" not in lowered.text
    assert "X0 (A B Y VDD VSS) child" in lowered.text


def test_extract_body_excludes_continued_subckt_header_ports(tmp_path: Path) -> None:
    source = tmp_path / "continued-header-body.scs"
    source.write_text(
        "subckt cell A B \\\n"
        "    Y VDD VSS\n"
        "X0 (A B Y VDD VSS) child\n"
        "ends cell\n",
        encoding="utf-8",
    )

    body = extract_subckt_body(load_netlist_snapshot(source), "cell")

    assert body == "X0 (A B Y VDD VSS) child"


def test_lowering_normalizes_continued_instance_statements_for_spicein(
    tmp_path: Path,
) -> None:
    source = tmp_path / "continued-instance.scs"
    source.write_text(
        "subckt cell A B Y VDD VSS\n"
        "X0 (A B Y \\\n"
        "    VDD VSS) child\n"
        "ends cell\n",
        encoding="utf-8",
    )

    lowered = lower_subckt_default_parameters(load_netlist_snapshot(source), "cell")

    assert lowered.interfaces == {"cell": ("A", "B", "Y", "VDD", "VSS")}
    assert "X0 (A B Y VDD VSS) child" in lowered.text
    assert "\\\n" not in lowered.text


def test_spectre_logical_lines_strip_comments_and_join_continuations() -> None:
    text = """\
// ignored
subckt cell A \\
    Y // interface continuation
M0 (Y A 0 0) nch_mac \\
    l=30n w=120n
ends cell
"""

    assert tuple(iter_spectre_logical_lines(text)) == (
        "subckt cell A Y",
        "M0 (Y A 0 0) nch_mac l=30n w=120n",
        "ends cell",
    )

    with pytest.raises(ValueError, match="unterminated"):
        tuple(iter_spectre_logical_lines("M0 (Y A 0 0) nch_mac \\"))


def test_snapshot_parses_a_continued_subckt_header_via_logical_lines(tmp_path: Path) -> None:
    source = tmp_path / "continued.scs"
    source.write_text(
        "subckt cell A \\\n"
        "// an ignored continuation line\n"
        "Y parameters drive=1\n"
        "ends cell\n",
        encoding="utf-8",
    )

    assert load_netlist_snapshot(source).interfaces == {"cell": ("A", "Y")}


def test_inline_pwl_tables_preserve_exact_ordered_tokens_for_oa_cdf(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pwl.scs"
    source.write_text(
        "subckt tb\n"
        "VSTEP (OUT 0) vsource type=pwl "
        "wave=[0 0 10p 0 11p 900m 20p 900m]\n"
        "VDC (VDD 0) vsource dc=900m\n"
        "ends tb\n",
        encoding="utf-8",
    )

    tables = parse_spectre_pwl_sources(source, "tb")

    assert tables[0].instance == "VSTEP"
    assert tables[0].points == (
        ("0", "0"),
        ("10p", "0"),
        ("11p", "900m"),
        ("20p", "900m"),
    )


def test_inline_pwl_rejects_tables_larger_than_native_analoglib_capacity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "too-large.scs"
    values = " ".join(f"{index}p {index}" for index in range(51))
    source.write_text(
        f"subckt tb\nV0 (OUT 0) vsource type=pwl wave=[{values}]\nends tb\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="50-pair"):
        parse_spectre_pwl_sources(source, "tb")


def test_recursive_hierarchy_counts_instances_and_omits_unreachable_cdl(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary.scs"
    primary.write_text(
        "subckt top A Y VSS\n"
        "X0 (A N VSS) child drive=2\n"
        "X1 (N Y VSS) child drive=2\n"
        "ends top\n"
        "subckt unreachable A Y\n"
        "ends unreachable\n",
        encoding="utf-8",
    )
    dependency = tmp_path / "dependency.scs"
    dependency.write_text(
        "subckt child A Y VSS parameters drive=1\n"
        "M0 (Y A VSS VSS) nch_mac l=30n w=120n nf=drive\n"
        "ends child\n",
        encoding="utf-8",
    )

    hierarchy = resolve_netlist_hierarchy(
        (load_netlist_snapshot(primary), load_netlist_snapshot(dependency)),
        top="top",
        primitive_masters=("nch_mac",),
    )

    assert hierarchy.reachable_counts == {"child": 2, "top": 1}
    assert hierarchy.primitive_counts == {"nch_mac": 2}
    assert hierarchy.direct_children["top"] == {"child": 2}
    assert hierarchy.unreachable_subckts == ("unreachable",)
    assert hierarchy.dependency_order == ("child", "top")
    cdl = render_canonical_cdl(hierarchy)
    assert cdl.index(".SUBCKT child") < cdl.index(".SUBCKT top")
    assert ".SUBCKT child A Y VSS PARAMS: drive=1" in cdl
    assert "X0 A N VSS child drive=2" in cdl
    assert "unreachable" not in cdl


def test_recursive_hierarchy_rejects_missing_master(tmp_path: Path) -> None:
    source = tmp_path / "missing.scs"
    source.write_text(
        "subckt top A Y\nX0 (A Y) undeclared_child\nends top\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing master undeclared_child"):
        resolve_netlist_hierarchy(
            (load_netlist_snapshot(source),),
            top="top",
            primitive_masters=("nch_mac",),
        )


def test_recursive_hierarchy_ignores_missing_master_in_unreachable_sibling(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unreachable-missing.scs"
    source.write_text(
        "subckt selected A Y\nends selected\n"
        "subckt unreachable A Y\nX0 (A Y) undeclared_child\nends unreachable\n",
        encoding="utf-8",
    )

    hierarchy = resolve_netlist_hierarchy(
        (load_netlist_snapshot(source),),
        top="selected",
        primitive_masters=(),
    )

    assert hierarchy.reachable_counts == {"selected": 1}
    assert hierarchy.unreachable_subckts == ("unreachable",)
    assert "unreachable" not in render_canonical_cdl(hierarchy)


def test_recursive_hierarchy_rejects_child_port_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "arity.scs"
    source.write_text(
        "subckt child A Y VSS\nends child\n"
        "subckt top A Y\nX0 (A Y) child\nends top\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="has 2 nodes; expected 3"):
        resolve_netlist_hierarchy(
            (load_netlist_snapshot(source),),
            top="top",
            primitive_masters=(),
        )


def test_recursive_hierarchy_rejects_cycles_and_duplicate_definitions(
    tmp_path: Path,
) -> None:
    cyclic = tmp_path / "cyclic.scs"
    cyclic.write_text(
        "subckt first A Y\nX0 (A Y) second\nends first\n"
        "subckt second A Y\nX0 (A Y) first\nends second\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="recursive subckt hierarchy"):
        resolve_netlist_hierarchy(
            (load_netlist_snapshot(cyclic),),
            top="first",
            primitive_masters=(),
        )

    duplicate = tmp_path / "duplicate.scs"
    duplicate.write_text("subckt first A Y\nends first\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate subckt definition first"):
        resolve_netlist_hierarchy(
            (load_netlist_snapshot(cyclic), load_netlist_snapshot(duplicate)),
            top="first",
            primitive_masters=(),
        )
