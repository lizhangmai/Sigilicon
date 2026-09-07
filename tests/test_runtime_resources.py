from __future__ import annotations

from pathlib import Path

import pytest

from sigilicon.execution._plan import RuntimeEnvironment
from sigilicon.execution._resources import Resources
from sigilicon.execution.runtime import preflight_environment


@pytest.mark.parametrize("kind", ["files", "directories"])
def test_runtime_preflight_rejects_symlinked_configured_paths(
    tmp_path: Path,
    kind: str,
) -> None:
    target = tmp_path / "target"
    if kind == "files":
        target.write_text("model\n", encoding="utf-8")
        link = tmp_path / "model"
        link.symlink_to(target)
    else:
        target.mkdir()
        link = tmp_path / "models"
        link.symlink_to(target, target_is_directory=True)

    resources = Resources(**{kind: {"pdk.resource": str(link)}})
    runtime = RuntimeEnvironment(**{kind: {"MODEL_PATH": "pdk.resource"}})

    checks = preflight_environment(runtime, resources)

    assert len(checks) == 1
    assert checks[0].status == "blocked"
