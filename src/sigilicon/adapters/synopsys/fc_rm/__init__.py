"""Versioned local RM assets and configuration translation.

Reference: Synopsys FC-RM V-2023.12. The flat non-DFT main scripts are
vendored locally, never sourced from the reference installation.
Adaptations: fail-fast sourcing, detached reference DBs, a final checkpoint
observation, and X-2025 empty-list argument handling. Scheduling, log
validation and artifact publication are Sigilicon responsibilities.
"""
from __future__ import annotations

from pathlib import Path
import shutil

from sigilicon.adapters.synopsys.fc_flow import FcAction, LABELS, RM_SCRIPTS
from sigilicon.adapters.synopsys.fc_library import reference_lef


def word(value: object) -> str:
    """One Tcl word, with no command/variable/backslash substitution."""
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("[", "\\[").replace("]", "\\]").replace("\n", "\\n").replace("\r", "\\r") + '"'


def materialize(root: Path, action: FcAction, context) -> Path:
    """Generate one supervised invocation from the sealed action and sources."""
    assets = Path(__file__).parent / "v2023_12"
    shutil.copytree(assets, root, dirs_exist_ok=True)
    generated = root / "generated"
    generated.mkdir()
    (root / "logs_fc").mkdir()
    def write(name, body):
        path = generated / name
        path.write_text(body + "\n")
        return str(path)
    def setting(name, value):
        return f"set {name} {word(value)}\n"

    # Source paths refer to the immutable, already sealed execution input tree.
    hdl = action.hdl
    read_rtl = "set_app_var search_path [concat $search_path [list " + " ".join(
        word(context.source_directory / p) for p in hdl.include_dirs) + "]]\n"
    read_rtl += "analyze -format sverilog "
    if hdl.defines:
        read_rtl += "-define [list " + " ".join(word(k + "=" + v) for k, v in hdl.defines) + "] "
    read_rtl += "[list " + " ".join(word(context.source_path(p)) for p in hdl.sources) + "]\n"
    read_rtl += "elaborate " + word(hdl.top)
    if action.parameters:
        read_rtl += " -parameters " + word(",".join(f"{k}={v}" for k,v in action.parameters))
    # Parameter overrides suffix the elaborated top name. Link the current
    # elaborated block, then normalize its name for RM checkpoint/export identity.
    read_rtl += "\nset_top_module\n"
    if action.parameters:
        read_rtl += "rename_block -to_block " + word(hdl.top)
    read_rtl += '''
foreach definition $SITE_DEFINITION_LIST {
    lassign $definition site width height symmetry
    if {![sizeof_collection [get_site_defs -quiet $site]]} {
        create_site_def -name $site -width $width -height $height -symmetry $symmetry
    }
}
'''
    rtl_path = write("read_rtl.tcl", read_rtl)
    if action.stage == "reference-library":
        write("reference.lef", reference_lef(
            context.resource_text(context.step.runtime.files["SIGILICON_FC_TECH_LEF"]),
            context.resource_text(context.step.runtime.files["SIGILICON_FC_CELL_LEF"])))

    parasitic_path = write("parasitics.tcl", """
set rc_map_args {}
if {[info exists ::env(SIGILICON_FC_RC_MAP)]} {
    set rc_map_args [list -layermap $::env(SIGILICON_FC_RC_MAP)]
}
read_parasitic_tech -tlup $::env(SIGILICON_FC_RC_EARLY) {*}$rc_map_args -name rc_early
read_parasitic_tech -tlup $::env(SIGILICON_FC_RC_LATE) {*}$rc_map_args -name rc_late
""")
    mcmm = "remove_scenarios -all\nremove_modes -all\nremove_corners -all\ncreate_mode functional\n"
    mcmm += "create_corner " + word(action.corner) + "\n"
    mcmm += "create_scenario -name " + word("functional_" + action.corner) + " -mode functional -corner " + word(action.corner) + "\n"
    mcmm += f"set_process_number {action.process}\nset_voltage {action.voltage}\nset_temperature {action.temperature}\n"
    mcmm += "set_parasitic_parameters -early_spec rc_early -late_spec rc_late\n"
    # Owner constraints may use FC collection commands beyond read_sdc's
    # restricted interpreter (for example macro clocks and slew annotation).
    mcmm += "source " + word(context.source_path(action.constraints)) + "\n"
    mcmm += "set_scenario_status [all_scenarios] -none -active true -setup true -hold true -dynamic_power true -leakage_power true -max_transition true -max_capacitance true\n"
    mcmm_path = write("mcmm.tcl", mcmm)
    setup = "".join(setting(k,v) for k,v in {
        "DESIGN_NAME": hdl.top, "DESIGN_LIBRARY": "design.dlib",
        "REPORTS_DIR": "./reports_fc", "OUTPUTS_DIR": "./outputs_fc",
        "DESIGN_STYLE": "flat", "INIT_DESIGN_INPUT": "RTL",
        "RTL_SOURCE_FORMAT": "script", "FC_RTL_READ_SCRIPT": rtl_path,
        "SET_HOST_OPTIONS_MAX_CORES": action.cores,
        "DFT_INSERT_ENABLE": "false", "DFT_PORTS_FILE": "",
        "TCL_MCMM_SETUP_FILE": mcmm_path, "TCL_PARASITIC_SETUP_FILE": parasitic_path,
        "TCL_FLOORPLAN_FILE": str(context.source_path(action.floorplan)),
        "TCL_MULTI_VT_CONSTRAINT_FILE": "",
        "WRITE_DATA_FROM_BLOCK_NAME": "chip_finish",
        "REPORT_QOR_REPORT_CONGESTION": "false", "REPORT_DISABLE_GUI": "true",
        "SET_QOR_STRATEGY_METRIC": action.metric,
    }.items())
    setup += """
set TECH_FILE $::env(SIGILICON_FC_TECH_FILE)
set FUSION_REFERENCE_LIBRARY_LEF_LIST [list ./generated/reference.lef]
set FUSION_REFERENCE_LIBRARY_DB_LIST [list $::env(SIGILICON_FC_CELL_DB)]
set FUSION_REFERENCE_LIBRARY_DIR ./references
# Clear example hooks; only the declared design/platform inputs are active.
foreach variable [info globals TCL_USER_*_SCRIPT] { set $variable "" }
proc sigilicon_reference_libraries {} {
    set result {}
    foreach path [glob -nocomplain ./references/*] {
        if {[string match {@@*} [file tail $path]]} { continue }
        if {[file isdirectory $path] && [file isfile $path/registry.dat]} {
            lappend result $path
        }
    }
    return [lsort $result]
}
set REFERENCE_LIBRARY [sigilicon_reference_libraries]
"""
    if action.stage != "reference-library":
        # References are explicit sealed artifacts, not RM Makefile discovery.
        # init_design concatenates that discovery list with REFERENCE_LIBRARY.
        setup += 'set FUSION_REFERENCE_LIBRARY_DIR ""\n'
    if action.pg_connections:
        setup += setting("TCL_USER_CONNECT_PG_NET_SCRIPT", context.source_path(action.pg_connections))
    macro_compile = 'set_app_var sh_continue_on_error false\n'
    for i, macro in enumerate(action.macros):
        macro_compile += 'read_lib ' + word(context.source_path(macro.liberty)) + '\n'
        macro_compile += 'write_lib ' + word(macro.library) + ' -format db -output ' + word(root / f'generated/macro_{i}.db') + '\n'
        setup += 'lappend FUSION_REFERENCE_LIBRARY_LEF_LIST ' + word(context.source_path(macro.lef)) + '\n'
        setup += 'lappend FUSION_REFERENCE_LIBRARY_DB_LIST ' + word(root / f'generated/macro_{i}.db') + '\n'
    if action.macros and action.stage == "reference-library":
        write('compile_macros.tcl', macro_compile + 'exit\n')
    # Child LC sources design_setup + header only: reference configuration must
    # live here, not solely in technology_override or a stage RM_VARFILE.
    setup_path = write("sealed_setup.tcl", setup)
    with (root / "rm_setup/design_setup.tcl").open("a") as stream:
        stream.write("\n# Sigilicon sealed design configuration\nsource " + word(setup_path) + "\n")
    # fc_setup/sidefile_setup reset defaults after design_setup. Reapply the
    # same sealed intent at RM's override seam, also used by later stages.
    technology = "source " + word(setup_path) + "\n" + """
set SITE_DEFINITION_LIST {}
source $::env(SIGILICON_FC_TECHNOLOGY_SETUP)
set WRITE_GDS_LAYER_MAP_FILE $::env(SIGILICON_FC_GDS_MAP)
"""
    # Rebind references before stage open/copy/link. No previous absolute
    # scratch location is treated as a valid dependency.
    if action.stage not in {"reference-library", "init"}:
        technology += """
open_lib $DESIGN_LIBRARY
set_ref_libs -ref_libs $REFERENCE_LIBRARY
close_lib
"""
    (root / "technology_override.tcl").write_text(technology)
    finalize = f"""
set expected_label {word(hdl.top + "/" + LABELS[action.stage])}
set stage_name {word(action.stage)}
"""
    if action.stage == "reference-library":
        finalize += """
set libs [sigilicon_reference_libraries]
if {![llength $libs]} {error "reference compilation produced no libraries"}
create_lib probe.dlib -technology $TECH_FILE -ref_libs $libs
close_lib
"""
    else:
        if action.stage == "init":
            # RM save_block -as makes a labeled copy without changing current.
            finalize += 'open_block $expected_label\n'
        finalize += 'file mkdir ${REPORTS_DIR}/${REPORT_PREFIX}\nset declared_macros {}\n'
        for macro in action.macros:
            finalize += 'set selected [get_cells -hierarchical -filter ' + word('ref_name == ' + macro.cell) + ']\n'
            finalize += f'if {{[sizeof_collection $selected] != {macro.instances}}} {{error "declared macro instance count mismatch: {macro.cell}"}}\n'
            finalize += 'set declared_macros [add_to_collection $declared_macros $selected]\n'
        finalize += """
set leaf_cells [get_cells -hierarchical -filter "is_hierarchical == false"]
set nonmacro_cells [remove_from_collection $leaf_cells $declared_macros]
set digital_cells [filter_collection $nonmacro_cells "is_physical_only == false"]
set physical_cells [filter_collection $nonmacro_cells "is_physical_only == true"]
set accounting [open ${REPORTS_DIR}/${REPORT_PREFIX}/macro_accounting w]
foreach {category cells} [list macro $declared_macros digital $digital_cells physical_only $physical_cells] {
    set area 0.0
    set missing 0
    foreach_in_collection cell $cells {
        set value [get_attribute -quiet $cell area]
        if {[string is double -strict $value]} { set area [expr {$area + $value}] } else { incr missing }
    }
    puts $accounting "${category}_count=[sizeof_collection $cells]"
    puts $accounting "${category}_area_unknown_count=$missing"
    if {$missing} { puts $accounting "${category}_area=unavailable" } else { puts $accounting "${category}_area=$area" }
}
close $accounting
"""
        if action.stage != "init":
            finalize += """
# Selected leaf-cell power includes their driven net switching (macro input
# capacitance included), never macro internal power. Activity is diagnostic.
redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/digital_power {
    report_power -cell_power $digital_cells -nosplit -significant_digits 6
}
"""
        if action.stage == "export":
            finalize += """
set REPORT_STAGE post_route
set REPORT_ACTIVE_SCENARIOS ""
source ./rm_fc_scripts/report_qor.tcl
redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/check_routes {check_routes}
"""
        finalize += """
if {[get_object_name [current_block]] ne [get_object_name [get_blocks $expected_label]]} {
    error "FC current block does not match expected stage label"
}
save_block
save_lib
close_lib
"""
    finalize += """
set result [open stage_complete.rpt w]
puts $result "stage=$stage_name"
puts $result "block=$expected_label"
close $result
"""
    write("finalize.tcl", finalize)
    launch = "cd " + word(root) + "\nset_lc_options -exec_path $::env(SIGILICON_SYNOPSYS_LC_SHELL)\n"
    launch += "source ./rm_fc_scripts/" + RM_SCRIPTS[action.stage] + ".tcl\n"
    write("launch.tcl", launch)
    runner = root / "run.sh"
    commands = 'set -eu\n'
    if action.macros and action.stage == 'reference-library':
        import shlex
        commands += '"$SIGILICON_SYNOPSYS_LC_SHELL" -f ' + shlex.quote(str(generated / 'compile_macros.tcl')) + '\n'
    commands += 'exec "$SIGILICON_SYNOPSYS_FC_SHELL" -batch -f "$SIGILICON_FC_LAUNCH" -output_log_file "$SIGILICON_FC_LOG"\n'
    runner.write_text(commands)
    return runner
