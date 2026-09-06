from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from conftest import write_component_owner, write_file, write_test_platform
from sigilicon.execution import RunStore
from sigilicon.project import Project


def _measurement_project(tmp_path: Path, mode: str, inputs: tuple[str, ...] = ("design.toml",)) -> Project:
    write_test_platform(tmp_path)
    owner = tmp_path / "ip/fixture"
    write_file(owner / "circuit.scs", "// circuit\n")
    write_file(owner / "spec.toml", "limit = 1\n")
    write_file(owner / "design.toml", 'cell = "fixture"\n')
    write_file(owner / "measure.py", r'''
def measurement(request):
    assert request["spec_path"] == "spec.toml"
    assert request["circuit_path"] == "circuit.scs"
    assert request["platform"] == "testpdk"
    assert request["inputs"]["design.toml"] == 'cell = "fixture"\n'
    if request["phase"] == "render":
        return {"deck": 'include "{{model}}"\ninclude "{{circuit}}"',
                "outputs": ["wave.prn"], "condition": {"corner": "tt"}}
    assert request["raw_outputs"]["wave.prn"] == "raw waveform\n"
    mode = request["parameters"]["mode"]
    if mode == "exception":
        raise ValueError("owner parser rejected the waveform")
    return {"measurements": {"passed": mode == "pass", "samples": 1},
            "normalized_csv": "time,value\n0,1\n"}
''')
    write_file(owner / "operations.toml", f'''schema = 5
contract_kind = "owner-operations"
path_scope = "owner"
owner = "fixture"
[operations.measure]
uses = "cadence.spectre"
filesets = [{{ component = "fixture", fileset = "measurement" }}]
config = {{ program = "measure.py", spec = "spec.toml", circuit = "circuit.scs", inputs = {json.dumps(inputs)}, platform = "testpdk", model_set = "nominal", parameters = {{ mode = "{mode}" }}, timeout_seconds = 10 }}
evidence = {{ role = "regression", level = "l1", scope = "fixture" }}
''')
    component = write_component_owner(tmp_path, "fixture", filesets={
        "measurement": tuple(f"ip/fixture/{name}" for name in ("operations.toml", "measure.py", "spec.toml", "circuit.scs", "design.toml")),
    })
    component.write_text(component.read_text().replace("[sources]", 'operation_catalog = "source_0"\n[sources]'))
    simulator = write_file(tmp_path / "bin/spectre", "#!/bin/sh\nprintf 'raw waveform\\n' > wave.prn\nprintf 'spectre completes with 0 errors\\n'\n", executable=True)
    with (tmp_path / "sigilicon.toml").open("a") as stream:
        stream.write(f'\n[runtime.tools]\n"cadence.spectre" = "{simulator}"\n"runtime.python" = "{sys.executable}"\n')
    return Project.open(tmp_path)


@pytest.mark.parametrize(("inputs", "error"), (
    (("design.toml", "design.toml"), "duplicates"),
    (("outside.toml",), "step filesets"),
    (("../design.toml",), "relative"),
))
def test_measurement_inputs_belong_to_the_selected_source_closure(
    tmp_path: Path, inputs: tuple[str, ...], error: str,
) -> None:
    project = _measurement_project(tmp_path, "pass", inputs)
    with pytest.raises(ValueError, match=error):
        project.plan("fixture:measure")


@pytest.mark.parametrize("mode", ("pass", "fail", "exception"))
def test_measurement_preserves_raw_outputs_before_owner_evaluation(tmp_path: Path, mode: str) -> None:
    project = _measurement_project(tmp_path, mode)
    plan = project.plan("fixture:measure")
    result = project.run(plan)
    assert result.status == ("succeeded" if mode == "pass" else "failed")
    stored = RunStore(project.artifact_root).read(
        owner="fixture", operation="measure", variant=None, run_id=result.run_id,
    )
    assert stored.status == result.status
    raw = next(artifact for outcome in result.outcomes for artifact in outcome.result.artifacts
               if artifact.kind == "raw.cadence-spectre")
    assert raw.path.read_text() == "raw waveform\n"
    if mode != "exception":
        evidence = next(artifact for outcome in result.outcomes for artifact in outcome.result.artifacts
                        if artifact.kind == "evidence.measurement")
        saved = json.loads(evidence.path.read_text())
        assert saved["plan_identity"] == plan.identity
        assert saved["measurements"]["passed"] is (mode == "pass")


def test_measurement_preserves_nested_model_includes_and_duplicate_basenames(tmp_path: Path, monkeypatch) -> None:
    _measurement_project(tmp_path, 'pass')
    platform = tmp_path / 'configs/platform/testpdk'
    for directory in ('nmos', 'pmos'):
        write_file(platform / directory / 'device.scs', f'// {directory} model\n')
    (platform / 'model.scs').write_text('include "nmos/device.scs"\ninclude "pmos/device.scs"\n')
    simulation = platform / 'simulation.toml'
    simulation.write_text(simulation.read_text() + 'support_files = ["nmos/device.scs", "pmos/device.scs"]\n')
    from sigilicon.external_tools import managed_process
    run_process = managed_process.run
    observed = []

    def inspect_process(request):
        if '-format' in request.argv:
            deck = Path(request.argv[-1]).read_text()
            entry = Path(deck.split('"')[1])
            for line in entry.read_text().splitlines():
                relative = line.split('"')[1]
                observed.append((entry.parent / relative).read_text())
        return run_process(request)

    monkeypatch.setattr(managed_process, 'run', inspect_process)
    project = Project.open(tmp_path)
    result = project.run(project.plan('fixture:measure'))
    assert result.status == 'succeeded'
    assert observed == ['// nmos model\n', '// pmos model\n']
