from __future__ import annotations

import os
from pathlib import Path

from sigilicon.workflows.spectre import find_spectre


def test_find_spectre_preserves_the_public_launcher_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "spectre-real"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    launcher = tmp_path / "spectre"
    launcher.symlink_to(target.name)
    monkeypatch.setenv("VB_SPECTRE_BIN", str(launcher))

    result = find_spectre()

    assert result == Path(os.path.abspath(launcher))
    assert result.is_symlink()
