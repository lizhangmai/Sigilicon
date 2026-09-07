##########################################################################################
# Version: V-2023.12
# Copyright (C) 2014-2024 Synopsys, Inc. All rights reserved.
##########################################################################################

if {[get_app_var synopsys_program_name] == "fc_shell" && [get_app_var synopsys_shell_mode] == "frontend"}  {
   puts "RM-warning: Using the Fusion Compiler Frontend Shell. Design Planning commands will require a special license in this shell mode. Please use the Unified Fusion Compiler shell."
}

set search_path "$search_path ./rm_user_plugin_scripts ./rm_tech_scripts ./rm_fc_scripts ./rm_setup ./examples"
if {$SUPPLEMENTAL_SEARCH_PATH != ""} {
   set search_path "$search_path $SUPPLEMENTAL_SEARCH_PATH"
}

if {$synopsys_program_name == "icc2_shell" || $synopsys_program_name == "fc_shell"} {
   set_host_options -max_cores $SET_HOST_OPTIONS_MAX_CORES

   ## The default number of significant digits used to display values in reports
   set_app_options -name shell.common.report_default_significant_digits -value 3 ;# tool default is 2
}

set sh_continue_on_error false ;# Sigilicon: fail-fast batch execution

if {![file exists $OUTPUTS_DIR]} {file mkdir $OUTPUTS_DIR} ;# do not change this line or directory may not be created properly
if {![file exists $REPORTS_DIR]} {file mkdir $REPORTS_DIR} ;# do not change this line or directory may not be created properly
if {$WRITE_QOR_DATA && ![file exists $WRITE_QOR_DATA_DIR]} {file mkdir $WRITE_QOR_DATA_DIR} ;# do not change this line or directory may not be created properly
if {$WRITE_QOR_DATA && ![file exists $COMPARE_QOR_DATA_DIR]} {file mkdir $COMPARE_QOR_DATA_DIR} ;# do not change this line or directory may not be created properly

##########################################################################################
## Message handling
##########################################################################################
if {[get_app_var synopsys_program_name] == "fc_shell" || [get_app_var synopsys_program_name] == "icc2_shell"} {
	suppress_message ATTR-11 ;# suppress the information about that design specific attribute values override over library values
	set_message_info -id PVT-012 -limit 1
	set_message_info -id PVT-013 -limit 1
}

##########################################################################################
## enable_runtime_improvements
##########################################################################################
## The following runs the default version of enable_runtime_improvements.
## To switch to a different version, list supported versions by 'enable_runtime_improvements -list_versions', then specify the version by using flow.runtime.version.
if {$synopsys_program_name == "fc_shell" || $synopsys_program_name == "icc2_shell"} {
    enable_runtime_improvements
}

puts "RM-info: Hostname: [sh hostname]"; puts "RM-info: Date: [date]"; puts "RM-info: PID: [pid]"; puts "RM-info: PWD: [pwd]"
