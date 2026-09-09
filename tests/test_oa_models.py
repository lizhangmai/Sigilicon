from pathlib import Path
import shutil

import pytest

from conftest import write_test_platform
from sigilicon.domain.platform import load_platform
from sigilicon.project import Project
from sigilicon.virtuoso.models import OaModelInputs


def model_inputs(tmp_path):
    write_test_platform(tmp_path)
    platform = tmp_path / "ip/fixture/configs/platform/testpdk"
    contract = platform / "simulation.toml"
    contract.write_text(contract.read_text() + 'support_files = ["devices/core.scs"]\n')
    (platform / "model.scs").write_text('include "devices/core.scs"\n')
    (platform / "devices").mkdir()
    (platform / "devices/core.scs").write_text("// planned device model\n")
    models = load_platform(
        Project.open(tmp_path), "fixture", "testpdk"
    ).simulation.default
    run = tmp_path / "artifacts/run"
    run.mkdir(parents=True)
    sealed = {}
    for index, source in enumerate(models.paths):
        path = run / str(index)
        path.write_bytes(source.read_bytes())
        sealed[source] = path
    return OaModelInputs.capture(models, sealed), run


def test_oa_models_survive_deletion_of_build_run(tmp_path):
    models, run = model_inputs(tmp_path)
    library = tmp_path / "virtuoso/lib"
    library.mkdir(parents=True)
    model = models.install(library)
    assert models.install(library) == model
    shutil.rmtree(run)

    with models.verify(library, [{"file": str(model), "section": "top_tt"}]) as proof:
        assert model.read_text() == 'include "devices/core.scs"\n'
        assert (model.parent / "devices/core.scs").read_text() == "// planned device model\n"
        assert proof["files"] == 2


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink", "old-path"])
def test_oa_models_reject_stale_or_modified_inputs(tmp_path, fault):
    models, _ = model_inputs(tmp_path)
    library = tmp_path / "virtuoso/lib"
    library.mkdir(parents=True)
    model = models.install(library)
    support = model.parent / "devices/core.scs"
    if fault == "missing":
        support.unlink()
    elif fault == "changed":
        support.write_text("// another device model\n")
    elif fault == "symlink":
        support.unlink()
        support.symlink_to(
            tmp_path / "ip/fixture/configs/platform/testpdk/devices/core.scs"
        )
    else:
        model = tmp_path / "deleted/run/model.scs"
    with pytest.raises((OSError, RuntimeError, ValueError)):
        with models.verify(library, [{"file": str(model), "section": "top_tt"}]):
            pytest.fail("invalid model input was accepted")


def test_oa_models_detect_changes_during_simulation(tmp_path):
    models, _ = model_inputs(tmp_path)
    library = tmp_path / "virtuoso/lib"
    library.mkdir(parents=True)
    model = models.install(library)
    with pytest.raises(RuntimeError, match="changed|modified"):
        with models.verify(library, [{"file": str(model), "section": "top_tt"}]):
            model.write_text("// changed while simulator was reading\n")


def test_oa_models_reject_same_name_from_another_planned_revision(tmp_path):
    old, run = model_inputs(tmp_path)
    library = tmp_path / "virtuoso/lib"
    library.mkdir(parents=True)
    old_model = old.install(library)
    platform = load_platform(Project.open(tmp_path), "fixture", "testpdk")
    sealed = {}
    for index, source in enumerate(platform.simulation.default.paths):
        path = run / f"revised-{index}"
        path.write_bytes(source.read_bytes() + b"// revised PDK\n")
        sealed[source] = path
    revised = OaModelInputs.capture(platform.simulation.default, sealed)
    revised_model = revised.install(library)

    assert old_model.read_bytes() != revised_model.read_bytes()
    with pytest.raises(RuntimeError, match="not bound to the current plan"):
        with revised.verify(library, [{"file": str(old_model), "section": "top_tt"}]):
            pytest.fail("old model revision was accepted")
