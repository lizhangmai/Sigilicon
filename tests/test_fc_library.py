from sigilicon.adapters.synopsys.fc_library import reference_lef


def test_reference_lef_retains_technology_vias_with_cell_geometry():
    tech = 'LAYER VIA1\n TYPE CUT ;\nEND VIA1\nVIA via_one DEFAULT\n LAYER M1 ;\n RECT 0 0 1 1 ;\nEND via_one\nEND LIBRARY\n'
    cells = 'VERSION 5.8 ;\nMACRO cell\n PIN A\n  PORT\n   VIA 0 0 via_one ;\n  END\n END A\nEND cell\nEND LIBRARY\n'
    result = reference_lef(tech, cells)
    assert 'VIA via_one DEFAULT\n LAYER M1 ;\n RECT 0 0 1 1 ;\nEND via_one' in result
    assert result.endswith(cells)
    assert result.count('END LIBRARY') == 1


def test_reference_lef_without_technology_vias_preserves_cells():
    assert reference_lef('LAYER M1\nEND M1\n', 'MACRO x\nEND x\n') == 'MACRO x\nEND x\n'
