from pathlib import Path

import pytest

from sigilicon.layout.technology import load_layout_technology


def _write_contract(path: Path) -> None:
    path.write_text(
        '''schema = 1
contract_kind = "test-layout-technology"
path_scope = "owner"
owner = "test-owner"

[model_polarities]
nch = "nmos"
pch = "pmos"

[layers]
routing1 = "M1"
routing2 = "M2"
routing3 = "M3"
diffusion = "OD"
p_implant = "PP"
n_implant = "NP"
n_well = "NW"

[vias]
substrate_tap = "M1_POD"
well_tap = "M1_NOD"
routing1_routing2 = "M2_M1"
routing2_routing3 = "M3_M2"

[via_landings.substrate_tap]
routing1 = [55, 55]

[via_landings.well_tap]
routing1 = [55, 55]

[via_landings.routing1_routing2]
routing1 = [55, 55]
routing2 = [55, 55]

[via_landings.routing2_routing3]
routing2 = [85, 130]
routing3 = [55, 55]

[mos_pcell]
length_parameter = "l"
width_parameter = "Wfg"
finger_count_parameter = "fingers"
source_terminal = "S"
drain_terminal = "D"
source_alias_prefix = "S_"
drain_alias_prefix = "D_"
cdf_callback_parameter = "routePolydir"
cdf_callback_bypass_parameters = ["polyContacts"]
gate_contact_value = "Bottom"
gate_contact_enhancement_parameter = "polyContactsEnh"
gate_contact_enhancement_value = "Bottom"

''',
        encoding="utf-8",
    )


def test_layout_technology_keeps_owner_schema_and_resolves_routing_roles(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "technology.toml"
    _write_contract(contract)

    technology = load_layout_technology(
        contract,
        contract_kind="test-layout-technology",
        owner="test-owner",
    )

    assert technology.polarity("pch") == "pmos"
    assert technology.layer("routing2") == "M2"
    assert technology.via("routing2_routing3") == "M3_M2"
    assert technology.via_landing_half_size(
        "routing2_routing3",
        "routing2",
    ) == (85, 130)


def test_layout_technology_applies_partial_via_landing_profile_overrides(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "technology.toml"
    _write_contract(contract)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "\n[mos_pcell]\n",
            "\n[via_landing_profiles.stacked.routing1_routing2]\n"
            "routing2 = [85, 130]\n\n[mos_pcell]\n",
        ),
        encoding="utf-8",
    )

    technology = load_layout_technology(
        contract,
        contract_kind="test-layout-technology",
        owner="test-owner",
    )

    assert technology.via_landing_half_size(
        "routing1_routing2", "routing2", profile="stacked"
    ) == (85, 130)
    assert technology.via_landing_half_size(
        "routing1_routing2", "routing1", profile="stacked"
    ) == (55, 55)


def test_layout_technology_requires_landing_policy_for_every_via(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "technology.toml"
    _write_contract(contract)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "[via_landings.well_tap]\nrouting1 = [55, 55]\n\n",
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="every and only"):
        load_layout_technology(
            contract,
            contract_kind="test-layout-technology",
            owner="test-owner",
        )


def test_layout_technology_rejects_profile_layers_not_used_by_via(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "technology.toml"
    _write_contract(contract)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "\n[mos_pcell]\n",
            "\n[via_landing_profiles.stacked.routing1_routing2]\n"
            "routing3 = [85, 130]\n\n[mos_pcell]\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-landing layers"):
        load_layout_technology(
            contract,
            contract_kind="test-layout-technology",
            owner="test-owner",
        )


def test_layout_technology_reads_common_roles_from_platform_domain_payload(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "layout.toml"
    _write_contract(contract)
    owner_payload = contract.read_text(encoding="utf-8").split(
        "owner = \"test-owner\"\n", 1
    )[1]
    contract.write_text(
        '''schema = 1
contract_kind = "platform-layout"
path_scope = "platform"
owner = "test-platform"
dbu_per_micron = 1000

[custom_layout]
'''
        + owner_payload.replace("\n[", "\n[custom_layout."),
        encoding="utf-8",
    )

    technology = load_layout_technology(
        contract,
        contract_kind="platform-layout",
        owner="test-platform",
        path_scope="platform",
        payload_key="custom_layout",
    )

    assert technology.owner == "test-platform"
    assert technology.layer("routing1") == "M1"


def test_layout_technology_loads_optional_passive_pcell_interfaces(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "technology.toml"
    _write_contract(contract)
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "\n[mos_pcell]\n",
            '''
[mom_pcell]
cell_name = "mom2t"
plus_terminal = "PLUS"
minus_terminal = "MINUS"
finger_width_parameter = "wf"
finger_spacing_parameter = "sf"
finger_length_parameter = "lf"
finger_count_parameter = "nf"
start_metal_parameter = "start"
stop_metal_parameter = "stop"
multiplicity_parameter = "m"

[resistor_pcell]
cell_name = "rm4"
plus_terminal = "PLUS"
minus_terminal = "MINUS"
length_parameter = "l"
width_parameter = "w"
calculation_parameter = "calc"
calculation_value = "l_&_w"
multiplicity_parameter = "m"

[mos_pcell]
''',
        ),
        encoding="utf-8",
    )

    technology = load_layout_technology(
        contract,
        contract_kind="test-layout-technology",
        owner="test-owner",
    )

    assert technology.require_mom_pcell().finger_count_parameter == "nf"
    assert technology.require_resistor_pcell().calculation_value == "l_&_w"
