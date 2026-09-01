from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.domain.repository import Project
from sigilicon.workflows import ip_packaging

from conftest import write_project_context


def _write_release(root: Path) -> Path:
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_text("payload\n", encoding="utf-8")
    manifest = {
        "schema": 1,
        "contract_kind": "ip-release-manifest",
        "release_kind": "source-package",
        "ip_name": "fixture",
        "release_id": "development-fixture",
        "exports": [{"name": "fixture"}],
        "views": [
            {
                "export": "fixture",
                "role": "payload",
                "path": "payload.txt",
                "size": payload.stat().st_size,
            }
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _skip_semantic_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ip_packaging, "_packaged_interface_check", lambda *_: None)
    monkeypatch.setattr(ip_packaging, "_packaged_maturity_check", lambda *_: None)


def test_exact_release_audit_rejects_symlinked_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)
    assert ip_packaging.audit_ip_release_manifest(manifest_path)["ip_name"] == "fixture"

    external = tmp_path / "external.txt"
    external.write_text("payload\n", encoding="utf-8")
    payload = manifest_path.parent / "payload.txt"
    payload.unlink()
    payload.symlink_to(external)

    with pytest.raises(RuntimeError, match="symlink"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_published_pointer_does_not_resolve_away_release_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)
    alias = tmp_path / "release-alias"
    alias.symlink_to(manifest_path.parent, target_is_directory=True)
    pointer = tmp_path / "current.json"
    pointer.write_text(
        json.dumps(
            {
                "ip_name": "fixture",
                "release_id": "development-fixture",
                "maturity": "development",
                "source_commit": "0" * 40,
                "manifest": "release-alias/manifest.json",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises((OSError, RuntimeError), match="symlink|Not a directory"):
        ip_packaging.load_published_ip(pointer, artifact_root=tmp_path)


def test_release_build_rejects_symlinked_namespace_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.from_file(write_project_context(tmp_path))
    exports = project.artifact_root / "exports"
    exports.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (exports / "fixture").symlink_to(outside, target_is_directory=True)
    contract = SimpleNamespace(project=project, collateral=())
    monkeypatch.setattr(ip_packaging, "load_ip_contract", lambda *_args, **_kw: contract)
    monkeypatch.setattr(
        ip_packaging,
        "_plan_loaded_ip_release",
        lambda *_args, **_kw: {
            "missing_items": [],
            "working_tree_dirty": False,
            "release_root": "exports/fixture/package/development-fixture",
        },
    )

    with pytest.raises(OSError):
        ip_packaging.build_ip_release(tmp_path / "release.toml", project=project)

    assert list(outside.iterdir()) == []
