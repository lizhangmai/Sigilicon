from __future__ import annotations

from sigilicon.workflows.layout_verification import (
    parse_drc_summary,
    parse_lvs_report,
    render_drc_run_deck,
    render_lvs_run_deck,
)


def test_drc_deck_resolution_disables_macro_inapplicable_options() -> None:
    source = """#DEFINE EFP // top
#DEFINE EFP
#DEFINE FULL_CHIP // top
#DEFINE WITH_SEALRING // top
#DEFINE WITH_APRDL // top
#DEFINE WITH_POLYIMIDE // top
#DEFINE GUIDELINE_ESD // top
#DEFINE GUIDELINE_ANALOG // top
#DEFINE CHECK_LOW_DENSITY // top
#DEFINE CHECK_LOW_DENSITY
LAYOUT PATH "GDSFILENAME"
LAYOUT PRIMARY "TOPCELLNAME"
DRC RESULTS DATABASE "DRC_RES.db"
DRC SUMMARY REPORT "DRC.rep"  // HIER
"""

    result = render_drc_run_deck(
        source,
        layout_path="layout.gds",
        primary="bit",
        results_path="results.db",
        summary_path="summary.rep",
        disabled_defines={
            "EFP": 2,
            "FULL_CHIP": 1,
            "WITH_SEALRING": 1,
            "WITH_APRDL": 1,
            "WITH_POLYIMIDE": 1,
            "GUIDELINE_ESD": 1,
            "GUIDELINE_ANALOG": 1,
            "CHECK_LOW_DENSITY": 2,
        },
    )

    assert "\n#DEFINE " not in "\n" + result
    assert 'LAYOUT PATH "layout.gds"' in result
    assert 'LAYOUT PRIMARY "bit"' in result
    assert 'DRC RESULTS DATABASE "results.db"' in result


def test_lvs_deck_resolution_binds_every_declared_output() -> None:
    source = """LAYOUT PRIMARY "lvs_top"
LAYOUT PATH "lvs_top.gds"
SOURCE PRIMARY "lvs_top"
SOURCE PATH "lvs_top.cdl"
DRC RESULTS DATABASE "calibre_drc.db" ASCII // ASCII or GDSII
DRC SUMMARY REPORT "calibre_drc.sum"
ERC RESULTS DATABASE "calibre_erc.db" ASCII // ASCII or GDSII
ERC SUMMARY REPORT "calibre_erc.sum"
LVS REPORT "lvs.rep"
  //MASK SVDB DIRECTORY "svdb" QUERY
  MASK SVDB DIRECTORY "svdb" QUERY
"""

    result = render_lvs_run_deck(
        source,
        layout_path="layout.gds",
        source_path="source.cdl",
        primary="bit",
        work_dir="work",
    )

    assert 'LAYOUT PRIMARY "bit"' in result
    assert 'SOURCE PRIMARY "bit"' in result
    assert 'LVS REPORT "work/lvs.rep"' in result
    assert 'MASK SVDB DIRECTORY "work/svdb" QUERY' in result


def test_drc_summary_separates_configuration_warnings_and_refuses_waivers() -> None:
    base = """LAYER SRAMDMY ............ TOTAL Original Geometry Count = {sram} ({sram})
LAYER SRM_3 ............... TOTAL Original Geometry Count = 0 (0)
RULECHECK EFP_rules_are_OFF:WARNING .... TOTAL Result Count = 1 (1)
RULECHECK IO_CONNECT_CORE_NET_VOLTAGE_IS_CORE:WARNING1 .... TOTAL Result Count = 1 (1)
RULECHECK DIODMY_L:WARNING .... TOTAL Result Count = 1 (1)
RULECHECK PO.R.10 .... TOTAL Result Count = 0 (0)
TOTAL DRC Results Generated:     3 (3)
"""

    options = {
        "configuration_warnings": (
            "EFP_rules_are_OFF:WARNING",
            "IO_CONNECT_CORE_NET_VOLTAGE_IS_CORE:WARNING1",
            "DIODMY_L:WARNING",
        ),
        "waiver_layers": ("SRAMDMY", "SRM_3"),
    }
    clean = parse_drc_summary(base.format(sram=0), **options)
    waived = parse_drc_summary(base.format(sram=1), **options)

    assert clean["passed"] is True
    assert clean["violation_count"] == 0
    assert clean["configuration_warning_count"] == 3
    assert waived["passed"] is False
    assert waived["waiver_layers_used"] is True


def test_drc_summary_accepts_foundry_total_that_excludes_warning_rulechecks() -> None:
    text = """--- RUNTIME WARNINGS
---
Missing connections STAMPing layer SD_00_11 by layer PSDc.
----------------------------------------------------------------------------------
--- ORIGINAL LAYER STATISTICS
---
LAYER SRAMDMY ............ TOTAL Original Geometry Count = 0 (0)
LAYER SRM_3 ............... TOTAL Original Geometry Count = 0 (0)
RULECHECK EFP_rules_are_OFF:WARNING .... TOTAL Result Count = 1 (1)
RULECHECK IO_CONNECT_CORE_NET_VOLTAGE_IS_CORE:WARNING1 .... TOTAL Result Count = 1 (1)
RULECHECK DIODMY_L:WARNING .... TOTAL Result Count = 1 (1)
RULECHECK M4.W.1 .... TOTAL Result Count = 4  (4)
TOTAL DRC Results Generated:     4 (4)
"""

    result = parse_drc_summary(
        text,
        configuration_warnings=(
            "EFP_rules_are_OFF:WARNING",
            "IO_CONNECT_CORE_NET_VOLTAGE_IS_CORE:WARNING1",
            "DIODMY_L:WARNING",
        ),
        waiver_layers=("SRAMDMY", "SRM_3"),
    )

    assert result["passed"] is False
    assert result["violation_count"] == 4
    assert result["configuration_warning_count"] == 3
    assert result["runtime_warning_count"] == 1


def test_lvs_report_requires_the_primary_cell_to_be_correct() -> None:
    result = parse_lvs_report("  CORRECT        bit           bit\n", primary="bit")

    assert result == {"passed": True, "comparison_result": "CORRECT"}
