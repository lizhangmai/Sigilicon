from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from sigilicon.cli.main import main as sigilicon_main
from sigilicon.project import Project
from sigilicon import release_store
from sigilicon.release_store import ReleaseRef, ReleaseStore
from sigilicon.workflows import ip_packaging

from conftest import write_project_context


def _write_release(root: Path) -> Path:
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_text("payload\n", encoding="utf-8")
    manifest = {
        "schema": 2,
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
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
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


def test_exact_release_audit_hashes_same_size_payload_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)
    payload = manifest_path.parent / "payload.txt"
    payload.write_text("PAYLOAD\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="content drifted"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_rejects_unknown_view_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["views"][0]["export"] = "undeclared"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="view metadata"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_rejects_unmanifested_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)
    (manifest_path.parent / "extra.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="inventory"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_rejects_unmanifested_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)
    (manifest_path.parent / "empty-extra").mkdir()

    with pytest.raises(RuntimeError, match="inventory"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_release_store_performs_one_audit_before_and_after_domain_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "temporary"
    manifest_path = _write_release(temporary)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    object_root = (
        tmp_path / "store" / "fixture" / "objects" / f"sha256-{digest}"
    )
    object_root.parent.mkdir(parents=True)
    temporary.rename(object_root)
    calls = 0
    original = release_store._audit_release_package

    def counted(path: Path, *, manifest_sha256: str | None = None):
        nonlocal calls
        calls += 1
        return original(path, manifest_sha256=manifest_sha256)

    validated: list[str] = []
    monkeypatch.setattr(release_store, "_audit_release_package", counted)
    result = ReleaseStore(tmp_path / "store").open(
        ReleaseRef("fixture", digest),
        validate=lambda package: validated.append(str(package.manifest["ip_name"])),
    )

    assert result.ref == ReleaseRef("fixture", digest)
    assert validated == ["fixture"]
    assert calls == 2


def test_release_audit_is_reachable_only_through_the_public_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    _skip_semantic_checks(monkeypatch)

    assert sigilicon_main(["release", "audit", str(manifest_path)]) == 0
    assert '"ip_name": "fixture"' in capsys.readouterr().out


def test_release_build_rejects_symlinked_namespace_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.open(write_project_context(tmp_path).parent)
    store_root = project.artifact_root / "release-store"
    store_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store_root / "fixture").symlink_to(outside, target_is_directory=True)
    contract = SimpleNamespace(project=project, collateral=())
    monkeypatch.setattr(ip_packaging, "load_ip_contract", lambda *_args, **_kw: contract)
    monkeypatch.setattr(
        ip_packaging,
        "_plan_loaded_ip_release",
        lambda *_args, **_kw: {
            "missing_items": [],
            "working_tree_dirty": False,
            "release_store": "fixture",
        },
    )

    with pytest.raises(OSError):
        ip_packaging.build_ip_release(tmp_path / "release.toml", project=project)

    assert list(outside.iterdir()) == []
