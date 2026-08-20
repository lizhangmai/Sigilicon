foreach variable {
    PPA_WORK_ROOT
    PPA_STDCELL_DB
    PPA_DW_FOUNDATION
    PPA_RTL_SOURCES
    PPA_TOP
    PPA_REFERENCE_FILTER
    PPA_EXPECTED_REFERENCE_INSTANCES
    PPA_COMPILE_CLOCK_PERIOD_NS
    PPA_REPORT_CLOCK_PERIOD_NS
    PPA_CLOCK_UNCERTAINTY_NS
    PPA_INPUT_DELAY_NS
    PPA_OUTPUT_DELAY_NS
    PPA_INPUT_STATIC_PROBABILITY
    PPA_INPUT_TOGGLE_RATE
    PPA_OUTPUT_LOAD
    PPA_MAP_EFFORT
    PPA_AREA_EFFORT
} {
    if {![info exists ::env($variable)] || $::env($variable) eq ""} {
        error "required synthesis environment variable $variable is not set"
    }
}

set work_root [file normalize $::env(PPA_WORK_ROOT)]
file mkdir $work_root
define_design_lib WORK -path [file join $work_root work]
set_app_var target_library [list $::env(PPA_STDCELL_DB)]
set_app_var synthetic_library [list $::env(PPA_DW_FOUNDATION)]
set_app_var link_library [list "*" $::env(PPA_STDCELL_DB) $::env(PPA_DW_FOUNDATION)]

set sources [split $::env(PPA_RTL_SOURCES) $::tcl_platform(pathSeparator)]
set defines {}
if {[info exists ::env(PPA_DEFINES)] && $::env(PPA_DEFINES) ne ""} {
    set defines [split $::env(PPA_DEFINES) ","]
}
if {[llength $defines] == 0} {
    set analyzed [analyze -format sverilog $sources]
} else {
    set analyzed [analyze -format sverilog -define $defines $sources]
}
if {!$analyzed} {
    echo "FATAL: RTL analysis failed"
    exit 2
}
if {![elaborate $::env(PPA_TOP)]} {
    echo "FATAL: elaboration failed"
    exit 2
}
current_design $::env(PPA_TOP)
if {![link]} {
    echo "FATAL: link failed"
    exit 2
}

proc apply_boundary_constraints {clock_period_ns} {
    set clock_name ppa_virtual_clock
    set clock_port ""
    if {[info exists ::env(PPA_CLOCK_PORT)] && $::env(PPA_CLOCK_PORT) ne ""} {
        set candidate [get_ports $::env(PPA_CLOCK_PORT) -quiet]
        if {[sizeof_collection $candidate] != 0} {
            set clock_port $candidate
            set clock_name $::env(PPA_CLOCK_PORT)
        }
    }
    if {[sizeof_collection [get_clocks $clock_name -quiet]] != 0} {
        remove_clock [get_clocks $clock_name]
    }
    if {$clock_port eq ""} {
        create_clock -name $clock_name -period $clock_period_ns
    } else {
        create_clock -name $clock_name -period $clock_period_ns $clock_port
    }
    if {$::env(PPA_CLOCK_UNCERTAINTY_NS) > 0.0} {
        set_clock_uncertainty $::env(PPA_CLOCK_UNCERTAINTY_NS) \
            [get_clocks $clock_name]
    }
    set excluded $clock_port
    if {[info exists ::env(PPA_RESET_PORTS)] && $::env(PPA_RESET_PORTS) ne ""} {
        set resets [get_ports [split $::env(PPA_RESET_PORTS) ","] -quiet]
        if {[sizeof_collection $resets] != 0} {
            if {$excluded eq ""} {
                set excluded $resets
            } else {
                set excluded [add_to_collection $excluded $resets]
            }
        }
    }
    set data_inputs [all_inputs]
    if {$excluded ne ""} {
        set data_inputs [remove_from_collection $data_inputs $excluded]
    }
    if {[sizeof_collection $data_inputs] != 0} {
        set_input_delay $::env(PPA_INPUT_DELAY_NS) -clock $clock_name $data_inputs
        set_switching_activity \
            -static_probability $::env(PPA_INPUT_STATIC_PROBABILITY) \
            -toggle_rate $::env(PPA_INPUT_TOGGLE_RATE) $data_inputs
    }
    if {[sizeof_collection [all_outputs]] != 0} {
        set_output_delay $::env(PPA_OUTPUT_DELAY_NS) \
            -clock $clock_name [all_outputs]
        set_load $::env(PPA_OUTPUT_LOAD) [all_outputs]
    }
}

apply_boundary_constraints $::env(PPA_COMPILE_CLOCK_PERIOD_NS)
set selected_cells [get_cells -hierarchical -quiet \
    -filter $::env(PPA_REFERENCE_FILTER)]
set selected_count [sizeof_collection $selected_cells]
set inventory [open [file join $work_root reference_inventory.rpt] w]
puts $inventory "total\t$selected_count"
foreach_in_collection cell $selected_cells {
    puts $inventory "[get_object_name $cell]\t[get_attribute $cell ref_name]"
}
close $inventory
if {$selected_count != $::env(PPA_EXPECTED_REFERENCE_INSTANCES)} {
    echo "FATAL: unexpected selected reference count $selected_count"
    exit 2
}

compile -map_effort $::env(PPA_MAP_EFFORT) -area_effort $::env(PPA_AREA_EFFORT)
apply_boundary_constraints $::env(PPA_REPORT_CLOCK_PERIOD_NS)
check_design > [file join $work_root check_design.rpt]
report_qor > [file join $work_root qor.rpt]
report_area -hierarchy > [file join $work_root area.rpt]
report_power > [file join $work_root power.rpt]
report_power -hierarchy > [file join $work_root power_hierarchy.rpt]
report_reference -hierarchy > [file join $work_root references.rpt]
report_timing -max_paths 20 > [file join $work_root timing.rpt]
write -format ddc -hierarchy -output [file join $work_root mapped.ddc]
write -format verilog -hierarchy -output [file join $work_root mapped.v]

quit
