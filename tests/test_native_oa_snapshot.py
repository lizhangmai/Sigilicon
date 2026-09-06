from __future__ import annotations
import json
from pathlib import Path

import pytest
from sigilicon.domain.oa_snapshot import NativeOaSnapshot


def test_native_snapshot_round_trip_and_content_identity(tmp_path: Path) -> None:
    view = tmp_path / "layout"
    view.mkdir()
    (view / "layout.oa").write_bytes(b"offline OA binary boundary fixture\x00")
    (view / "master.tag").write_text("layout.oa\n")
    snapshot = NativeOaSnapshot.capture(view, library="analog", cell="AMP", view="layout", technology_library="techLib")
    source = tmp_path / "view.json"
    source.write_text(json.dumps(snapshot.record))
    loaded = NativeOaSnapshot.load(source)
    assert loaded.identity == snapshot.identity
    loaded.verify(view)
    (view / "layout.oa").write_bytes(b"modified authoring content")
    with pytest.raises(ValueError, match="content drift"):
        loaded.verify(view)


@pytest.mark.parametrize("name", ["../layout.oa", "/layout.oa", "a//b", "layout.oa.cdslck", "a/../../b"])
def test_native_snapshot_rejects_unsafe_source_members(name: str) -> None:
    with pytest.raises(ValueError):
        NativeOaSnapshot("analog", "AMP", "layout", "techLib", {name: b"payload"})


def test_native_snapshot_rejects_a_symlink_inside_the_view(tmp_path: Path) -> None:
    view = tmp_path / "layout"
    view.mkdir()
    target = tmp_path / "outside.oa"
    target.write_bytes(b"outside source")
    (view / "layout.oa").symlink_to(target)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        NativeOaSnapshot.capture(view, library="analog", cell="AMP", view="layout", technology_library="techLib")
