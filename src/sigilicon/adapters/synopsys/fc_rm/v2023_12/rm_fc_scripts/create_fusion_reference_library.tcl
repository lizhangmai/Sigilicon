##########################################################################################
# Script: create_fusion_reference_library.tcl
# Version: V-2023.12
# Copyright (C) 2014-2024 Synopsys, Inc. All rights reserved.
##########################################################################################

source ./rm_utilities/procs_global.tcl
source ./rm_utilities/procs_fc.tcl
rm_source -file ./rm_setup/design_setup.tcl
if {$HPC_CORE != ""} {
	rm_source -file ./rm_setup/design_setup.tcl -after_file flow_override.tcl
}

rm_source -file ./rm_setup/fc_setup.tcl
rm_source -file ./rm_setup/header_fc.tcl
if {[file exists [which config_setup.tcl]]} {
	rm_source -file config_setup.tcl -after_file technology_override.tcl
} else {
	rm_source -file sidefile_setup.tcl -after_file technology_override.tcl
}
if {$HPC_CORE != ""} {rm_source -file ./rm_hpc_core_scripts/sidefile_setup_hpc_core.tcl}

## Create fusion library
if {$FUSION_REFERENCE_LIBRARY_FRAM_LIST != "" && $FUSION_REFERENCE_LIBRARY_DB_LIST != ""} {
	if {$FUSION_REFERENCE_LIBRARY_LEF_LIST != ""} {
		puts "RM-warning: Both FUSION_REFERENCE_LIBRARY_FRAM_LIST and FUSION_REFERENCE_LIBRARY_LEF_LIST are specified. Only one of them will be used. Using FUSION_REFERENCE_LIBRARY_FRAM_LIST instead."
	}
	puts "RM-info: Creating fusion lib based on input FUSION_REFERENCE_LIBRARY_FRAM_LIST and FUSION_REFERENCE_LIBRARY_DB_LIST. "
	if {[file exists $FUSION_REFERENCE_LIBRARY_DIR]} {
		puts "RM-info: FUSION_REFERENCE_LIBRARY_DIR ($FUSION_REFERENCE_LIBRARY_DIR) is specified and exists. The directory will be overwritten."
	}

	lc_sh {\
		source ./rm_setup/design_setup.tcl; \
		source ./rm_setup/header_fc.tcl; \
		compile_fusion_lib -copy -frame $FUSION_REFERENCE_LIBRARY_FRAM_LIST \
		-dbs $FUSION_REFERENCE_LIBRARY_DB_LIST \
		-log_file_dir $FUSION_REFERENCE_LIBRARY_LOG_DIR \
		-output_directory $FUSION_REFERENCE_LIBRARY_DIR \
		-force
	}
} elseif {$FUSION_REFERENCE_LIBRARY_LEF_LIST != "" && $FUSION_REFERENCE_LIBRARY_DB_LIST != "" } {
	puts "RM-info: Creating fusion lib based on input FUSION_REFERENCE_LIBRARY_LEF_LIST and FUSION_REFERENCE_LIBRARY_DB_LIST. "
	if {[file exists $FUSION_REFERENCE_LIBRARY_DIR]} {
		puts "RM-info: FUSION_REFERENCE_LIBRARY_DIR ($FUSION_REFERENCE_LIBRARY_DIR) is specified and exists. The directory will be overwritten."
	}

	lc_sh {\
		source ./rm_setup/design_setup.tcl; \
		source ./rm_setup/header_fc.tcl; \
		compile_fusion_lib -copy -lefs $FUSION_REFERENCE_LIBRARY_LEF_LIST \
		-dbs $FUSION_REFERENCE_LIBRARY_DB_LIST \
		-technology $TECH_FILE \
		-log_file_dir $FUSION_REFERENCE_LIBRARY_LOG_DIR \
		-output_directory $FUSION_REFERENCE_LIBRARY_DIR \
		-force
	}
} else {
	puts "RM-error: either FUSION_REFERENCE_LIBRARY_FRAM_LIST, FUSION_REFERENCE_LIBRARY_LEF_LIST, or FUSION_REFERENCE_LIBRARY_DB_LIST is not specified. Fusion library creation is skipped!"
}

echo [date] > create_fusion_reference_library
# Sigilicon: observe and close the exact completed checkpoint.
source ./generated/finalize.tcl
exit
