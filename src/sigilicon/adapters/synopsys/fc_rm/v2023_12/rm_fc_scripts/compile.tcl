##########################################################################################
# Script: compile.tcl
# Version: V-2023.12
# Copyright (C) 2014-2024 Synopsys, Inc. All rights reserved.
##########################################################################################

## NAME : FC_STEP_LIST
## DESCRIPTION : Task variable that configures the list of steps executed by compile_fusion
##               pre_initial_map - executes pre initial_map design setup steps
##               initial_map - compile_fusion is executed from initial_map to initial_map
##               logic_opto - compile_fusion is executed from logic_opto to logic_opto
##               insert_dft - Assumes logic opto complete, only insert_dft is executed
##               initial_place - compile_fusion is executed from initial_place to initial_place
##               initial_drc - compile_fusion is executed from initial_drc to initial_drc
##               initial_opto - compile_fusion is executed from initial_opto to initial_opto
##               initial_opto_incremental - compile_fusion  executes  initial_opto in incremental mode
##               final_place - compile_fusion is executed from final_place to final_place
##               final_opto - compile_fusion is executed from final_opto to final_opto
## TYPE : OOS
## VALUE : compile compile_through_initial_opto compile_logic_opto insert_dft compile_final

set FC_STEP_LIST "pre_initial_map initial_map logic_opto insert_dft initial_place initial_drc initial_opto final_place final_opto"

source ./rm_utilities/procs_global.tcl
source ./rm_utilities/procs_fc.tcl
rm_source -file ./rm_setup/design_setup.tcl
rm_source -file ./rm_setup/fc_dp_setup.tcl
rm_source -file ./rm_setup/fc_setup.tcl
rm_source -file ./rm_setup/header_fc.tcl
if {[file exists [which config_setup.tcl]]} {
	rm_source -file config_setup.tcl -after_file technology_override.tcl
} else {
	rm_source -file sidefile_setup.tcl -after_file technology_override.tcl
}
if {$HPC_CORE != ""} {
	rm_source -file ./flow_override.tcl
	rm_source -file ./rm_hpc_core_scripts/sidefile_setup_hpc_core.tcl
}
set PREVIOUS_STEP $INIT_DESIGN_BLOCK_NAME
set CURRENT_STEP $COMPILE_BLOCK_NAME
if { [info exists env(RM_VARFILE)] } {
	if { [file exists $env(RM_VARFILE)] } {
		rm_source -file $env(RM_VARFILE)
	} else {
		puts "RM-error: env(RM_VARFILE) specified but not found"
	}
}
set REPORT_PREFIX $CURRENT_STEP
file mkdir ${REPORTS_DIR}/${REPORT_PREFIX}
puts "RM-info: PREVIOUS_STEP = $PREVIOUS_STEP"
puts "RM-info: CURRENT_STEP  = $CURRENT_STEP"

redirect -tee -file ${REPORTS_DIR}/${REPORT_PREFIX}/run_start.rpt {run_start}

set_svf ${OUTPUTS_DIR}/${COMPILE_BLOCK_NAME}_init.svf

rm_source -file $TCL_PVT_CONFIGURATION_FILE -optional -print "TCL_PVT_CONFIGURATION_FILE"

open_lib ${DESIGN_LIBRARY}
copy_block -from ${DESIGN_NAME}/${PREVIOUS_STEP} -to ${DESIGN_NAME}/${CURRENT_STEP}
current_block ${DESIGN_NAME}/${CURRENT_STEP}

if {$SET_QOR_STRATEGY_MODE == "early_design"} {
    ## Automatically enable lenient policy for early_design mode
    set_early_data_check_policy -policy lenient -if_not_exist
} elseif {$EARLY_DATA_CHECK_POLICY != "none"} {
    ## Design check manager
    set_early_data_check_policy -policy $EARLY_DATA_CHECK_POLICY -if_not_exist
}

link_block

## The following only applies to hierarchical designs
## Swap abstracts if design view is used
if {$DESIGN_STYLE == "hier"} {
	if {$USE_ABSTRACTS_FOR_BLOCKS != ""} {
		puts "RM-info: Swapping [lindex $BLOCK_ABSTRACT_FOR_COMPILE 0] for abstracts for all blocks."
		change_abstract -references $USE_ABSTRACTS_FOR_BLOCKS -label [lindex $BLOCK_ABSTRACT_FOR_COMPILE 0] -view [lindex $BLOCK_ABSTRACT_FOR_COMPILE 1]
		report_abstracts
	}
	## Promote clock tree exceptions from blocks to top
	if {$USE_ABSTRACTS_FOR_BLOCKS != "" && $PROMOTE_CLOCK_BALANCE_POINTS} {
		rm_source -file $PROMOTE_ABSTRACT_CLOCK_DATA_FILE -print "PROMOTE_ABSTRACT_CLOCK_DATA_FILE"
	}
}

## For top and intermediate level of hierarchical designs, check the editability of the sub-blocks;
### if the editability is true for any sub-block, set it to false
### In RM, we are setting the editability of all the sub-blocks to false in the init_design.tcl script
### The following check is implemented to ensure that the editability of the sub-blocks is set to false in flows not running the init_design.tcl script
if {$USE_ABSTRACTS_FOR_BLOCKS != ""} {
        foreach_in_collection c [get_blocks -hierarchical] {
                if {[get_editability -blocks ${c}] == true } {
                set_editability -blocks ${c} -value false
                }
        }
        report_editability -blocks [get_blocks -hierarchical]
}

## Set active scenarios for the step (please include CTS and hold scenarios for CCD) ;
if {$COMPILE_ACTIVE_SCENARIO_LIST != ""} {
	set_scenario_status -active false [get_scenarios -filter active]
	set_scenario_status -active true $COMPILE_ACTIVE_SCENARIO_LIST
}

## Adjustment file for modes/corners/scenarios/models to applied to each step (optional)
rm_source -file $TCL_MODE_CORNER_SCENARIO_MODEL_ADJUSTMENT_FILE -optional -print "TCL_MODE_CORNER_SCENARIO_MODEL_ADJUSTMENT_FILE"

if {[sizeof_collection [get_scenarios -filter "hold && active"]] == 0} {
	puts "RM-warning: No active hold scenario is found. Recommended to enable hold scenarios here such that CCD skewing can consider them."
	puts "RM-info: Please activate hold scenarios for compile_fusion if they are available."
}

rm_source -file $TCL_LIB_CELL_PURPOSE_FILE -optional -print "TCL_LIB_CELL_PURPOSE_FILE"

##########################################################################################
## Settings
##########################################################################################
if {$MAX_ROUTING_LAYER != ""} {set_ignored_layers -max_routing_layer $MAX_ROUTING_LAYER} else { puts "RM-info: MAX_ROUTING_LAYER not defined, the highest techfile metal layer will be used by default" }
if {$MIN_ROUTING_LAYER != ""} {set_ignored_layers -min_routing_layer $MIN_ROUTING_LAYER} else { puts "RM-info: MIN_ROUTING_LAYER not defined, the lowest techfile metal layer will be used by default" }

## Non-persistent settings to be applied in each step (optional)
rm_source -file $TCL_USER_NON_PERSISTENT_SCRIPT -optional -print "TCL_USER_NON_PERSISTENT_SCRIPT"

## Multi Vt constraint file to be applied in each step (optional)
rm_source -file $TCL_MULTI_VT_CONSTRAINT_FILE -optional -print "TCL_MULTI_VT_CONSTRAINT_FILE"

## set_qor_strategy : a commmand which folds various settings of placement, optimization, timing, CTS, and routing, etc.
## - To query the target metric being set, use the "get_attribute [current_design] metric_target" command
set set_qor_strategy_cmd "set_qor_strategy -stage compile_initial -metric \"${SET_QOR_STRATEGY_METRIC}\" -mode \"${SET_QOR_STRATEGY_MODE}\""
if {$ENABLE_REDUCED_EFFORT} {
   lappend set_qor_strategy_cmd -reduced_effort
   puts "RM-info: When reduced_effort is enabled, high effort timing is always disabled"
} elseif {(!$ENABLE_REDUCED_EFFORT && $ENABLE_HIGH_EFFORT_TIMING)} {
   lappend set_qor_strategy_cmd -high_effort_timing
}
if {$SET_QOR_STRATEGY_CONGESTION_EFFORT != ""} {lappend set_qor_strategy_cmd -congestion_effort $SET_QOR_STRATEGY_CONGESTION_EFFORT}
puts "RM-info: Running $set_qor_strategy_cmd"
redirect -tee -file ${REPORTS_DIR}/${REPORT_PREFIX}/set_qor_strategy {eval ${set_qor_strategy_cmd} -report_only}
eval ${set_qor_strategy_cmd}

set rm_lib_type [get_attribute -quiet [current_design] rm_lib_type]

## Prefix
set_app_options -name opt.common.user_instance_name_prefix -value compile_
set_app_options -name cts.common.user_instance_name_prefix -value compile_cts_

## For set_qor_strategy -metric leakage_power, disabling the dynamic power analysis in active scenarios for optimization
# Scenario power analysis will be renabled after optimization for reporting
if {$SET_QOR_STRATEGY_METRIC == "leakage_power"} {
   set rm_dynamic_scenarios [get_object_name [get_scenarios -filter active==true&&dynamic_power==true]]

   if {[llength $rm_dynamic_scenarios] > 0} {
      puts "RM-info: Disabling dynamic analysis for $rm_dynamic_scenarios"
      set_scenario_status -dynamic_power false [get_scenarios $rm_dynamic_scenarios]
  }
}

## The following only applies to designs with physical hierarchy
### Ignore the sub-blocks (bound to abstracts) internal timing paths
if {$DESIGN_STYLE == "hier" && $PHYSICAL_HIERARCHY_LEVEL != "bottom"} {
        set_timing_paths_disabled_blocks -all_sub_blocks
}

##########################################################################################
## Additional setup
##########################################################################################
## CTS primary corner
## CTS automatically picks a corner with worst delay as the primary corner for its compile stage,
## which inserts buffers to balance clock delays in all modes of the corner;
## this setting allows you to manually specify a corner for the tool to use instead
if {$PREROUTE_CTS_PRIMARY_CORNER != ""} {
	puts "RM-info: Setting cts.compile.primary_corner to $PREROUTE_CTS_PRIMARY_CORNER (tool default unspecified)"
	set_app_options -name cts.compile.primary_corner -value $PREROUTE_CTS_PRIMARY_CORNER
}

## HPC_CORE specific
if {$HPC_CORE != "" } {
        set HPC_STAGE "compile"
        puts "RM-info: HPC_CORE is being set to $HPC_CORE; Loading HPC settings for stage $HPC_STAGE"
        redirect -tee -file ${REPORTS_DIR}/${REPORT_PREFIX}/${DESIGN_NAME}.${HPC_STAGE}.set_hpc_options {set_hpc_options -core $HPC_CORE -stage $HPC_STAGE -report_only}
        set_hpc_options -core $HPC_CORE -stage $HPC_STAGE
}

puts "RM-info: Checking for propagated clocks"
set currentMode [current_mode]
foreach_in_collection mode [all_modes] {
    current_mode $mode
    if {[regexp true [get_attribute [all_clocks] propagated_clock]]} {
       set clock_tree [remove_from_collection [all_fanout -flat -clock_tree] [all_registers -clock_pins]]
       if { [sizeof_collection $clock_tree] > 0 } {
           remove_propagated_clock [get_pins -hierarchical]
           remove_propagated_clock [get_ports]
           remove_propagated_clock [get_clocks -filter !is_virtual]
       }
    }
}
current_mode $currentMode

if { [lsearch $FC_STEP_LIST "pre_initial_map"] >= 0} {
	###########################################################################################
	## Pre-compile customizations
	###########################################################################################
	set_svf ${OUTPUTS_DIR}/${COMPILE_BLOCK_NAME}_pre_map.svf
	rm_source -file $TCL_USER_COMPILE_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_PRE_SCRIPT"

	#  Read the test model for the subblocks (used with "top" or "intermediate" hierarchical blocks)
	foreach ctl $CTL_FOR_ABSTRACT_BLOCKS {
		read_test_model $ctl
	}


	## The following app option is required if auto ungroup is disabled during compile
	if {![get_app_option_value -name compile.flow.autoungroup ]} {
	   puts "RM-info: Setting opt.common.consider_port_direction true as compile.flow.autoungroup is set to false"
	   set_app_options -name opt.common.consider_port_direction -value true
	}

	##########################################################################################
	## Create MV cells
	##########################################################################################
	# create_mv_cells is optional as MV cells are automatically inserted during compile
	puts "RM-info: Running create_mv_cells"
	create_mv_cells -verbose

	##########################################################################################
	## Checks
	##########################################################################################
	redirect -tee -file ${REPORTS_DIR}/${REPORT_PREFIX}/report_app_options.start {report_app_options -non_default *}
	redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/report_lib_cell_purpose {report_lib_cell -objects [get_lib_cells] -column {full_name:20 valid_purposes}}
        if {$SAIF_FILE_LIST != "" || $SET_QOR_STRATEGY_METRIC == "total_power"} {
	   set rm_dynamic_scenarios [get_object_name [get_scenarios -filter active==true&&dynamic_power==true]]
	   if {[llength $rm_dynamic_scenarios] > 0} {
                redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/report_activity.driver.start {report_activity -driver -scenarios $rm_dynamic_scenarios}
	   } else {
		puts "RM-info: The design does not have any active scenarios with dynamic_power analysis enabled. Skipping report_activity."
	   }
	}
	redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/check_variants.start {check_variants -dont_use -included_purposes}

        puts "RM-info: Running compile_fusion -check_only"
        redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/compile_fusion.check_only {compile_fusion -check_only}
	##########################################################################################
	## Retiming
	##########################################################################################
	# set_optimize_registers -modules [get_modules ...]
}

###########################################################################
### Indesign PrimePower
###########################################################################
if {([check_license -quiet "Fusion-Compiler-BE-NX"] || [check_license -quiet "Fusion-Compiler-NX"]) && [llength $TCL_PRIMEPOWER_CONFIG_FILE]> 0  && [llength $INDESIGN_PRIMEPOWER_STAGES] > 0} {
	reset_switching_activity -non_essential
	## Specify Indesign PrimePower confguration needed per your design
	## Example for Indesign PrimePower config :             examples/TCL_PRIMEPOWER_CONFIG_FILE.indesign_options.tcl
	rm_source -file $TCL_PRIMEPOWER_CONFIG_FILE -print "INDESIGN_PRIMEPOWER_STAGES requires a proper TCL_PRIMEPOWER_CONFIG_FILE"
}

if { [lsearch $FC_STEP_LIST "initial_map"] >= 0} {
	##########################################################################################
	## Initial Compile
	##########################################################################################
	set REPORT_STAGE mapped
	set_svf ${OUTPUTS_DIR}/${COMPILE_BLOCK_NAME}_initial_map.svf
	if {$DESIGN_STYLE == "hier" && ($PHYSICAL_HIERARCHY_LEVEL == "top" || $PHYSICAL_HIERARCHY_LEVEL == "intermediate")} {
	    set_app_options -name compile.auto_floorplan.enable -value false
	}

	## Specify set_scan_element false and set_wrapper_configuration -reuse_threshold commands
	#  prior to compile_fusion -to logic_opto command for an in_compile DFT flow
	rm_source -file $TCL_DFT_PRE_IN_COMPILE_SETUP_FILE -optional -print "TCL_DFT_PRE_IN_COMPILE_SETUP_FILE"

	set compile_cmd "compile_fusion -to initial_map"
	puts "RM-info: Running ${compile_cmd}"
	eval ${compile_cmd}

	rm_source -file $TCL_USER_COMPILE_INITIAL_MAP_POST_SCRIPT -optional -print "TCL_USER_COMPILE_INITIAL_MAP_POST_SCRIPT"
	if {$ENABLE_FUSA} {
	   rm_source -file $TCL_FUSA_POST_MAP_SETUP_FILE -optional -print "TCL_FUSA_POST_MAP_SETUP_FILE"
	}
	save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_initial_map
}

if { [lsearch $FC_STEP_LIST "logic_opto"] >= 0} {
	set_svf ${OUTPUTS_DIR}/${COMPILE_BLOCK_NAME}_logic_opto.svf

	## Mark clock network as ideal
	puts "RM-info: Marking clock network as ideal"
	set currentMode [current_mode]
	foreach_in_collection mode [all_modes] {
	    current_mode $mode
	    set clock_tree [remove_from_collection [all_fanout -flat -clock_tree] [all_registers -clock_pins]]
	    if { [sizeof_collection $clock_tree] > 0 } {
	        set_ideal_network $clock_tree
	    }
	}
	current_mode $currentMode

	rm_source -file $TCL_USER_COMPILE_LOGIC_OPTO_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_LOGIC_OPTO_PRE_SCRIPT"

	set compile_cmd "compile_fusion -from logic_opto -to logic_opto"
	puts "RM-info: Running ${compile_cmd}"
	eval ${compile_cmd}

	rm_source -file $TCL_USER_COMPILE_LOGIC_OPTO_POST_SCRIPT -optional -print "TCL_USER_COMPILE_LOGIC_OPTO_POST_SCRIPT"
	save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_logic_opto
	report_qor -summary

        ###########################################################################
        ### Indesign PrimePower
        ###########################################################################
        if {[llength $TCL_PRIMEPOWER_CONFIG_FILE] > 0  && [lsearch $INDESIGN_PRIMEPOWER_STAGES "AFTER_LOGIC_OPTO"] >= 0 } {
		set update_indesign_cmd "update_indesign_activity"
		if {$KEEP_INDESIGN_SAIF_FILE} {lappend update_indesign_cmd -keep saif -saif_suffix compile_logic_opto}
                puts "RM-info: Running ${update_indesign_cmd}"
                eval ${update_indesign_cmd}
        }
}

if { [lsearch $FC_STEP_LIST "insert_dft"] >= 0 } {
	set REPORT_STAGE mapped
	##########################################################################################
	##  DFT Insertion and Apply Mapped Netlist Constraints
	##########################################################################################
	set_svf ${OUTPUTS_DIR}/${COMPILE_BLOCK_NAME}_insert_dft.svf
	rm_source -file $TCL_USER_DFT_SETUP_PRE_SCRIPT -optional -print "TCL_USER_DFT_SETUP_PRE_SCRIPT"

	if { $DFT_INSERT_ENABLE } {
	  puts "RM-info: DFT_INSERT_ENABLE is enabled. Adding DFT."
	  if {![rm_source -file $TCL_USER_DFT_REPLACEMENT_SCRIPT -optional -print "TCL_USER_DFT_REPLACEMENT_SCRIPT"]} {
	     rm_source -file $DFT_SETUP_FILE -print "DFT_SETUP_FILE"
	     puts "RM-info: Running create_test_protocol"
	     create_test_protocol
	     redirect -tee ${REPORTS_DIR}/${REPORT_PREFIX}/initial_opto.pre-insert_dft.dft_drc {dft_drc -test_mode all_dft}
	     puts "RM-info: Running run_test_point_analysis"
	     redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/pre-insert_dft.run_test_point_analysis { run_test_point_analysis }
	     redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/pre-insert_dft.report_dft { report_dft }

	     # In "in-compile" DFT insertion flow, insert_dft command inserts the DFTMAX Codec and performs scan stitching.
	     # Use the preview_dft command to report on the DFTMAX Codec and scan chain structures prior to actual insertion

	     puts "RM-info: Running preview_dft"
	     redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/preview_dft { preview_dft }
	     puts "RM-info: Running insert_dft"
	     insert_dft

	     ## Special netlist and spf for early TMAX work
	     ## write test protocols for each DFT mode
	     foreach mode [all_test_modes] {
	         redirect -tee ${REPORTS_DIR}/${REPORT_PREFIX}/$COMPILE_BLOCK_NAME.initial_opto.$mode.dft_drc {dft_drc -test_mode $mode}
	         write_test_protocol -test_mode $mode -output $OUTPUTS_DIR/$COMPILE_BLOCK_NAME.$mode.spf
	     }

	     ### write_verilog for comparison with a DC netlist (no pg, no physical only cells, and no diodes)
	     set write_verilog_dc_cmd "write_verilog -compress gzip -exclude {scalar_wire_declarations leaf_module_declarations pg_objects end_cap_cells well_tap_cells filler_cells pad_spacer_cells physical_only_cells cover_cells diode_cells} -hierarchy all ${OUTPUTS_DIR}/$DESIGN_NAME.dc.v"
	     puts "RM-info: running $write_verilog_dc_cmd"
	     eval ${write_verilog_dc_cmd}
	   }
	   rm_source -file $TCL_USER_DFT_POST_SCRIPT -optional -print "TCL_USER_DFT_POST_SCRIPT"
           save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_insert_dft
	} else {

	  puts "RM-info: DFT_INSERT_ENABLE is disabled. No dft will be applied."

	}
}

if { [lsearch $FC_STEP_LIST "initial_place"] >= 0 || ([lsearch $FC_STEP_LIST "initial_opto_incremental"] >= 0 && [sizeof_collection [get_flat_cells -filter "!is_hard_macro&&!is_placed"]] > 0) } {
	set REPORT_STAGE synthesis
	set_svf ${OUTPUTS_DIR}/${COMPILE_BLOCK_NAME}.svf
	if {$DESIGN_STYLE == "hier" && $PHYSICAL_HIERARCHY_LEVEL == "bottom"} {
	    set_app_options -name compile.auto_floorplan.enable -value false
	}

	##########################################################################################
	## FUSA Safety Register Clock Splitting
	##########################################################################################
	if {$ENABLE_FUSA} {
	   if { [get_safety_register_groups] != "" } {
	     if { ($FUSA_CLOCK_SPLIT_BUF != "") && ($FUSA_CLOCK_SPLIT_INV != "") } {
	       puts "RM-info: Performing TMR clock Splitting"
	       insert_redundant_trees \
	         -safety_register_groups [get_safety_register_groups] \
	         -buffer_lib_cell  $FUSA_CLOCK_SPLIT_BUF \
	         -inverter_lib_cell $FUSA_CLOCK_SPLIT_INV \
	         -pin_types {clock scan reset}
	     }
	   }
	}

	##########################################################################################
	## Clock NDR modeling at compile_fusion
	##########################################################################################
	## Clock NDRs are created in settings.compile.tcl.
	## mark_clock_trees makes compile_fusion recognize them to model the congestion impact when trial CTS is not run.
	puts "RM-info: Running mark_clock_trees -routing_rules to model clock NDR impact during compile_fusion"
	mark_clock_trees -routing_rules

	if {$HPC_CORE != "" } {
	    rm_source -file $HPC_CONSTRAINTS_POST_DFT -optional -print $HPC_CONSTRAINTS_POST_DFT
	}

	## Spare cell insertion before initial_place
	rm_source -file $TCL_USER_SPARE_CELL_PRE_SCRIPT -optional -print "TCL_USER_SPARE_CELL_PRE_SCRIPT"

	##########################################################################################
        ## compile_fusion - initial_place to initial_opto
        ##########################################################################################
	rm_source -file $TCL_USER_COMPILE_INITIAL_PLACE_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_INITIAL_PLACE_PRE_SCRIPT"

	set compile_cmd "compile_fusion -from initial_place -to initial_place"
	puts "RM-info: Running ${compile_cmd}"
	eval ${compile_cmd}
        save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_initial_place
        if {$HPC_CORE != ""} {rm_source -file $HPC_INITIAL_PLACE_POST_SCRIPT -print "HPC_INITIAL_PLACE_POST_SCRIPT"}

        if { [lsearch $FC_STEP_LIST "initial_opto_incremental"] >= 0} {
            lappend FC_STEP_LIST initial_drc
        }

	##########################################################################
	## Regular MSCTS in compile
	##########################################################################
	if {$CTS_STYLE == "MSCTS" && $DESIGN_STYLE != "hier"} {
		if {[rm_source -file $TCL_REGULAR_MSCTS_FILE -print "TCL_REGULAR_MSCTS_FILE"]} {
		## Note : the following executes only if TCL_REGULAR_MSCTS_FILE is sourced
			set_app_options -name compile.flow.enable_multisource_clock_trees -value true
	                save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_MSCTS
		}
	} elseif {$CTS_STYLE != "standard"} {
		puts "RM-error: Specified CTS_STYLE($CTS_STYLE) is not supported, standard will be used."
	}
}

if { [lsearch $FC_STEP_LIST "initial_drc"] >= 0} {
        rm_source -file $TCL_USER_COMPILE_INITIAL_DRC_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_INITIAL_DRC_PRE_SCRIPT"

        set compile_cmd "compile_fusion -from initial_drc -to initial_drc"
        puts "RM-info: Running ${compile_cmd}"
        eval ${compile_cmd}

	report_qor -summary
	save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_initial_drc

	if { [lsearch $FC_STEP_LIST "initial_opto_incremental"] >= 0} {
	    lappend FC_STEP_LIST initial_opto
	}
}

if { [lsearch $FC_STEP_LIST "initial_opto"] >= 0} {
	set REPORT_STAGE synthesis
	rm_source -file $TCL_USER_COMPILE_INITIAL_OPTO_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_INITIAL_OPTO_PRE_SCRIPT"

	set compile_cmd "compile_fusion -from initial_opto -to initial_opto"
	puts "RM-info: Running ${compile_cmd}"
	eval ${compile_cmd}
	save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_initial_opto
	save_block
} elseif {[lsearch $FC_STEP_LIST "initial_opto_incremental"] >= 0} {
	rm_source -file $TCL_USER_COMPILE_INCREMENTAL_INITIAL_OPTO_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_INCREMENTAL_INITIAL_OPTO_PRE_SCRIPT"
	set compile_cmd "compile_fusion -from initial_opto -to initial_opto -incremental"
	puts "RM-info: Running ${compile_cmd}"
	eval ${compile_cmd}
	save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_initial_opto_incremental
}
if { [lsearch $FC_STEP_LIST "initial_opto"] >= 0 || [lsearch $FC_STEP_LIST "initial_opto_incremental"] >= 0} {
	report_qor -summary

	###########################################################################
	### Indesign PrimePower
	###########################################################################
	if {[llength $TCL_PRIMEPOWER_CONFIG_FILE]> 0  && [lsearch $INDESIGN_PRIMEPOWER_STAGES "AFTER_INITIAL_OPTO"] >= 0} {
		set update_indesign_cmd "update_indesign_activity"
	        if {$KEEP_INDESIGN_SAIF_FILE} {lappend update_indesign_cmd -keep saif -saif_suffix compile_initial_opto}
	        puts "RM-info: Running ${update_indesign_cmd}"
	        eval ${update_indesign_cmd}
	}
}

##########################################################################################
## compile_fusion from final_place
##########################################################################################
if { [lsearch $FC_STEP_LIST "final_place"] >= 0 } {
	set REPORT_STAGE synthesis

	set set_qor_strategy_cmd "set_qor_strategy -stage compile_final_place -metric \"${SET_QOR_STRATEGY_METRIC}\" -mode \"${SET_QOR_STRATEGY_MODE}\""
	if {$SET_QOR_STRATEGY_CONGESTION_EFFORT != ""} {lappend set_qor_strategy_cmd -congestion_effort $SET_QOR_STRATEGY_CONGESTION_EFFORT}
	puts "RM-info: Running $set_qor_strategy_cmd"
	redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/set_qor_strategy.compile_final_place {eval ${set_qor_strategy_cmd} -report_only}
	eval ${set_qor_strategy_cmd}

	## HPC_CORE specific
	if {$HPC_CORE != "" } {
		set HPC_STAGE "compile_place"
                puts "RM-info: HPC_CORE is being set to $HPC_CORE; Loading HPC settings for stage $HPC_STAGE"
                redirect -tee -file ${REPORTS_DIR}/${REPORT_PREFIX}/${DESIGN_NAME}.${HPC_STAGE}.set_hpc_options {set_hpc_options -core $HPC_CORE -stage $HPC_STAGE -report_only}
		set_hpc_options -core $HPC_CORE -stage $HPC_STAGE
	}

        rm_source -file $TCL_USER_COMPILE_FINAL_PLACE_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_FINAL_PLACE_PRE_SCRIPT"

        set compile_cmd "compile_fusion -from final_place -to final_place"
        puts "RM-info: Running ${compile_cmd}"
        eval ${compile_cmd}
        save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_final_place
}

if { [lsearch $FC_STEP_LIST "final_opto"] >= 0 } {
        rm_source -file $TCL_USER_COMPILE_FINAL_OPTO_PRE_SCRIPT -optional -print "TCL_USER_COMPILE_FINAL_OPTO_PRE_SCRIPT"

	set compile_cmd "compile_fusion -from final_opto -to final_opto"
        puts "RM-info: Running ${compile_cmd}"
        eval ${compile_cmd}

        report_qor -summary
        ###########################################################################
	### Indesign PrimePower
	###########################################################################
	if {[llength $TCL_PRIMEPOWER_CONFIG_FILE]> 0  && [lsearch $INDESIGN_PRIMEPOWER_STAGES "AFTER_FINAL_OPTO"] >= 0} {
                set update_indesign_cmd "update_indesign_activity"
                if {$KEEP_INDESIGN_SAIF_FILE} {lappend update_indesign_cmd -keep saif -saif_suffix compile_final_opto}
                puts "RM-info: Running ${update_indesign_cmd}"
                eval ${update_indesign_cmd}
	}

        ###########################################################################################
        ## Post-compile customizations
        ###########################################################################################
        rm_source -file $TCL_USER_COMPILE_POST_SCRIPT -optional -print "TCL_USER_COMPILE_POST_SCRIPT"

	## Spare cell insertion after final_opto
	rm_source -file $TCL_USER_SPARE_CELL_POST_SCRIPT -optional -print "TCL_USER_SPARE_CELL_POST_SCRIPT"

	save_block -as ${DESIGN_NAME}/${COMPILE_BLOCK_NAME}_final_opto
}

if {![rm_source -file $TCL_USER_CONNECT_PG_NET_SCRIPT -optional -print "TCL_USER_CONNECT_PG_NET_SCRIPT"]} {
## Note : the following executes if TCL_USER_CONNECT_PG_NET_SCRIPT is not sourced
	connect_pg_net
        # For non-MV designs with more than one PG, you should use connect_pg_net in manual mode.
}
## Re-enable power analysis if disabled for set_qor_strategy -metric leakage_power
if {[info exists rm_dynamic_scenarios] && [llength $rm_dynamic_scenarios] > 0} {
   puts "RM-info: Reenabling dynamic power analysis for $rm_dynamic_scenarios"
   set_scenario_status -dynamic_power true [get_scenarios $rm_dynamic_scenarios]
}

## Change names
if {$DEFINE_NAME_RULES_OPTIONS != ""} {
	eval define_name_rules verilog $DEFINE_NAME_RULES_OPTIONS
}
redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/report_name_rules.log {report_name_rules}
redirect -file ${REPORTS_DIR}/${REPORT_PREFIX}/report_names.log {report_names -rules verilog}
change_names -rules verilog -hierarchy -skip_physical_only_cells

if {$UPF_MODE == "golden"} {
        set upf_files "${UPF_FILE}"
        if {[file exists [which ${UPF_UPDATE_SUPPLY_SET_FILE}]]} { lappend upf_files "${UPF_UPDATE_SUPPLY_SET_FILE}" }
        write_ascii_files -force \
            -output ${OUTPUTS_DIR}/${CURRENT_STEP}.ascii_files \
            -golden_upf "${upf_files}"
} else {
        write_ascii_files -force \
            -output ${OUTPUTS_DIR}/${CURRENT_STEP}.ascii_files
}
saif_map -type ptpx -essential -write_map ${OUTPUTS_DIR}/${CURRENT_STEP}.saif.ptpx.map
saif_map -write_map ${OUTPUTS_DIR}/${CURRENT_STEP}.saif.fc.map

################################################################
## FUSA Setup
################################################################
if {$ENABLE_FUSA} {
  save_ssf ${OUTPUTS_DIR}/${CURRENT_STEP}.ssf
}
save_block

####################################
### Create abstract and frame
#####################################
### Enabled for hierarchical designs; for bottom and intermediate levels of physical hierarchy
if {$HPC_CORE != ""} {set_scenario_status [get_scenarios $ALL_SCENARIOS] -active true}
if {$DESIGN_STYLE == "hier" && $PHYSICAL_HIERARCHY_LEVEL != "top" && !$SKIP_ABSTRACT_GENERATION} {
        if {$USE_ABSTRACTS_FOR_POWER_ANALYSIS == "true"} {
                if {$HPC_CORE != ""} { rm_source -file $HPC_REPORT_QOR_POWER -optional -print "HPC_REPORT_QOR_POWER" }
                set_app_options -name abstract.annotate_power -value true
        }
        if { $PHYSICAL_HIERARCHY_LEVEL == "bottom" } {
                create_abstract -read_only
                create_frame -block_all true
        } elseif { $PHYSICAL_HIERARCHY_LEVEL == "intermediate"} {
            if { $ABSTRACT_TYPE_FOR_MPH_BLOCKS == "nested"} {
                ## Create nested abstract for the intermediate level of physical hierarchy
                create_abstract -read_only
                create_frame -block_all true
            } elseif { $ABSTRACT_TYPE_FOR_MPH_BLOCKS == "flattened"} {
                ## Create flattened abstract for the intermediate level of physical hierarchy
                create_abstract -read_only -preserve_block_instances false
                create_frame -block_all true
            }
        }
}

write_test_model  -output ${OUTPUTS_DIR}/${DESIGN_NAME}.ctl

set_svf -off

###########################################################################################
## Report and output
###########################################################################################
if {$REPORT_QOR} {
	set REPORT_ACTIVE_SCENARIOS $REPORT_COMPILE_ACTIVE_SCENARIO_LIST
	if {$REPORT_PARALLEL_SUBMIT_COMMAND != ""} {
		## Generate a file to pass necessary RM variables for running report_qor.tcl to the report_parallel command
		rm_generate_variables_for_report_parallel -work_directory ${REPORTS_DIR}/${REPORT_PREFIX} -file_name rm_tcl_var.tcl

		## Parallel reporting using the report_parallel command (requires a valid REPORT_PARALLEL_SUBMIT_COMMAND)
		report_parallel -work_directory ${REPORTS_DIR}/${REPORT_PREFIX} -submit_command ${REPORT_PARALLEL_SUBMIT_COMMAND} -max_cores ${REPORT_PARALLEL_MAX_CORES} -user_scripts [list "${REPORTS_DIR}/${REPORT_PREFIX}/rm_tcl_var.tcl" "[which report_qor.tcl]"]
	} else {
		## Classic reporting
		rm_source -file report_qor.tcl
	}
}

redirect -tee -file ${REPORTS_DIR}/${REPORT_PREFIX}/run_end.rpt {run_end}

write_qor_data -report_list "performance host_machine report_app_options" -label $REPORT_PREFIX -output $WRITE_QOR_DATA_DIR

report_msg -summary
print_message_info -ids * -summary
if { [info exists rm_compile_logic] } {
        echo [date] > compile_logic
} elseif { [info exists rm_compile_final] || [info exists rm_compile_final_single_fp] } {
        echo [date] > compile_final
} elseif  {[llength $FC_STEP_LIST] == 9 || [llength $FC_STEP_LIST] == 7} {
        echo [date] > compile
        rm_logparse $LOGS_DIR/compile.log
} elseif {[lsearch $FC_STEP_LIST "initial_map"] >= 1 && [llength $FC_STEP_LIST] == 3 } {
        echo [date] > compile_pre_dft
	rm_logparse $LOGS_DIR/compile_pre_dft.log
} elseif {[lsearch $FC_STEP_LIST "insert_dft"] >= 0 && [llength $FC_STEP_LIST] == 1} {
        echo [date] > compile_dft
	rm_logparse $LOGS_DIR/compile_dft.log
} else {
	echo [date] > compile_post_dft
	echo [date] > compile
        rm_logparse $LOGS_DIR/compile_post_dft.log
}

# Sigilicon: observe and close the exact completed checkpoint.
source ./generated/finalize.tcl
exit
