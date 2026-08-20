from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.ams.spec import load_ams_spec
from sigilicon.domain.design import load_design_spec
from sigilicon.workflows.spec_execution import execute_standalone_spec


def test_spec_loads_source_interface_and_pdk(project_factory) -> None:
    root, path = project_factory()

    spec = load_ams_spec(path, project_root=root)

    assert spec.design.port_order == ("IN", "OUT", "VDD", "VSS")
    assert spec.design.pdk.oa.technology_library == "techLib"
    assert spec.design.pdk.oa.reference_libraries == ("deviceLib",)
    assert spec.design.source_netlist == root / "ip/legacy" / "inv" / "circuit.scs"
    assert spec.design.sync_mode == "recursive"
    assert spec.simulation.interface.load_cap == "2f"
    assert spec.simulation.backends.ade.errpreset == "conservative"


def test_spec_rejects_backend_specific_legacy_load_name(project_factory) -> None:
    root, path = project_factory()
    text = path.read_text(encoding="utf-8").replace(
        'load_cap = "2f"',
        'standalone_load_cap = "2f"',
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="standalone_load_cap is obsolete"):
        load_ams_spec(path, project_root=root)


def test_spec_rejects_order_that_disagrees_with_source(project_factory) -> None:
    root, path = project_factory(port_order='"OUT", "IN", "VDD", "VSS"')

    with pytest.raises(ValueError, match="does not match"):
        load_ams_spec(path, project_root=root)


def test_spec_rejects_non_table_vector_without_leaking_attribute_error(
    project_factory,
) -> None:
    root, path = project_factory()
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("[[vectors]]\ninputs = [1]", "vectors = [1]\n#"), encoding="utf-8")

    with pytest.raises(ValueError):
        load_ams_spec(path, project_root=root)


def test_spec_rejects_generated_cell_collision_with_dut(project_factory) -> None:
    root, path = project_factory()
    text = path.read_text(encoding="utf-8").replace(
        'testbench = "tb_inv"', 'testbench = "inv"'
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="collide"):
        load_ams_spec(path, project_root=root)


def test_spec_rejects_wrapper_collision_with_source_subckt(project_factory) -> None:
    root, path = project_factory()
    source = root / "ip/legacy" / "inv" / "circuit.scs"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nsubckt inv_ams IN OUT\nR0 (IN OUT) resistor r=1k\nends inv_ams\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inv_ams"):
        load_ams_spec(path, project_root=root)


def test_design_spec_supports_explicit_multi_domain_supplies(project_factory) -> None:
    root, path = project_factory()
    path = path.with_name("design.toml")
    source = root / "ip/legacy" / "inv" / "circuit.scs"
    source.write_text(
        "subckt inv IN OUT VDDA VDDD VSS\nends inv\n",
        encoding="utf-8",
    )
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('supplies = ["VDD", "VSS"]', (
            'supplies = ["VDDA", "VDDD", "VSS"]\n'
            'primary_supply = "VDDD"\n'
            'ground_supply = "VSS"'
        ))
        .replace(
            'order = ["IN", "OUT", "VDD", "VSS"]',
            'order = ["IN", "OUT", "VDDA", "VDDD", "VSS"]',
        )
        .replace(
            'VDD = "inputOutput"',
            'VDDA = "inputOutput"\nVDDD = "inputOutput"',
        ),
        encoding="utf-8",
    )

    spec = load_design_spec(path, project_root=root)

    assert spec.supplies == ("VDDA", "VDDD", "VSS")
    assert spec.primary_supply == "VDDD"
    assert spec.ground_supply == "VSS"


def test_design_spec_rejects_ambiguous_multi_domain_supplies(project_factory) -> None:
    root, path = project_factory()
    path = path.with_name("design.toml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'supplies = ["VDD", "VSS"]',
            'supplies = ["VDDA", "VDD", "VSS"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="multi-domain"):
        load_design_spec(path, project_root=root)


def test_design_spec_accepts_only_declared_sync_modes(project_factory) -> None:
    root, path = project_factory()
    design = path.with_name("design.toml")
    design.write_text(
        design.read_text(encoding="utf-8").replace(
            'pdk = "testpdk"', 'pdk = "testpdk"\nsync_mode = "target-only"'
        ),
        encoding="utf-8",
    )
    assert load_design_spec(design, project_root=root).sync_mode == "target-only"

    design.write_text(
        design.read_text(encoding="utf-8").replace(
            'sync_mode = "target-only"', 'sync_mode = "ad-hoc"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sync_mode"):
        load_design_spec(design, project_root=root)


def test_standalone_spec_attests_oa_before_simulation_when_client_is_supplied(
    monkeypatch, project_factory
) -> None:
    root, path = project_factory()
    events: list[object] = []
    expected_result = object()

    def attest(paths, client, *, project_root):
        events.append(("audit", tuple(paths), client, project_root))
        return {"passed": True}

    def simulate(spec, *, xrun, timeout):
        events.append(("simulate", spec.design.path, xrun, timeout))
        return expected_result

    monkeypatch.setattr("sigilicon.workflows.spec_execution.attest_design_set", attest)
    monkeypatch.setattr("sigilicon.workflows.spec_execution.run_standalone", simulate)
    client = object()

    execution = execute_standalone_spec(
        path,
        root,
        xrun=None,
        timeout=123,
        oa_client=client,
    )

    assert execution.result is expected_result
    assert events == [
        ("audit", (root / "ip/legacy" / "inv" / "design.toml",), client, root),
        ("simulate", root / "ip/legacy" / "inv" / "design.toml", None, 123),
    ]


def test_standalone_spec_never_simulates_after_oa_attestation_failure(
    monkeypatch, project_factory
) -> None:
    root, path = project_factory()
    simulated = False

    def reject(*_args, **_kwargs):
        raise RuntimeError("stale OA")

    def simulate(*_args, **_kwargs):
        nonlocal simulated
        simulated = True

    monkeypatch.setattr("sigilicon.workflows.spec_execution.attest_design_set", reject)
    monkeypatch.setattr("sigilicon.workflows.spec_execution.run_standalone", simulate)

    with pytest.raises(RuntimeError, match="stale OA"):
        execute_standalone_spec(
            path,
            root,
            xrun=None,
            timeout=123,
            oa_client=object(),
        )

    assert simulated is False
