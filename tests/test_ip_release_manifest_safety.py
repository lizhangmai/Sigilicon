from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from sigilicon.cli.main import main as sigilicon_main
from sigilicon.release_store import ReleaseRef, ReleaseStore
from sigilicon.adapters.release import ip_packaging


def _write_release(root: Path) -> Path:
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_text("module fixture(input wire a); endmodule\n", encoding="utf-8")
    interface = root / "interface.toml"
    interface.write_text('''[module]
name = "fixture"
source = "payload.txt"
ports = [{ name = "a", direction = "input", width = 1 }]
''')
    availability = {"simulation": True, "synthesis": False, "physical_implementation": False}
    manifest = {
        "schema": 5,
        "contract_kind": "ip-release-manifest",
        "release_kind": "source-package",
        "ip_name": "fixture",
        "release_id": "development-fixture",
        "source_commit": "a" * 40,
        "exports": [{
            "name": "fixture",
            "interface": {"kind": "rtl", "bindings": {"interface_contract": "interface_contract"}, "contract": "interface.toml", "module": "fixture", "source_view": "payload"},
            "maturity": {"required_views": ["interface_contract", "payload"], "missing_items": []},
            "availability": availability,
        }],
        "maturity": {"level": "development", "checks": [{"name": "interface", "passed": True}], "missing_items": []},
        "availability": availability,
        "views": [{
            "export": "fixture", "name": role, "role": role, "path": path.name, "source": path.name,
            "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "format": view_format, "module": "fixture" if role == "payload" else None,
            "capabilities": ["simulation"] if role == "payload" else [],
        } for role, path, view_format in (("payload", payload, "verilog"), ("interface_contract", interface, "toml"))],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_exact_release_audit_rejects_symlinked_payload(
    tmp_path: Path
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    assert ip_packaging.audit_ip_release_manifest(manifest_path)["ip_name"] == "fixture"

    external = tmp_path / "external.txt"
    external.write_bytes((manifest_path.parent / "payload.txt").read_bytes())
    payload = manifest_path.parent / "payload.txt"
    payload.unlink()
    payload.symlink_to(external)

    with pytest.raises(RuntimeError, match="symlink"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_hashes_same_size_payload_tampering(
    tmp_path: Path
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    payload = manifest_path.parent / "payload.txt"
    payload.write_text(payload.read_text().replace("wire", "WIRE"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="content drifted"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_rejects_unknown_view_export(
    tmp_path: Path
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["views"][0]["export"] = "undeclared"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="view metadata"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_rejects_unmanifested_files(
    tmp_path: Path
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    (manifest_path.parent / "extra.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="inventory"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_exact_release_audit_rejects_unmanifested_directories(
    tmp_path: Path
) -> None:
    manifest_path = _write_release(tmp_path / "release")
    (manifest_path.parent / "empty-extra").mkdir()

    with pytest.raises(RuntimeError, match="inventory"):
        ip_packaging.audit_ip_release_manifest(manifest_path)


def test_release_store_rejects_mutation_during_domain_validation(
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "temporary"
    manifest_path = _write_release(temporary)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    object_root = (
        tmp_path / "store" / "fixture" / "objects" / f"sha256-{digest}"
    )
    object_root.parent.mkdir(parents=True)
    temporary.rename(object_root)
    validated: list[str] = []

    def mutate(package) -> None:
        validated.append(str(package.manifest["ip_name"]))
        (package.manifest_path.parent / "injected.txt").write_text(
            "changed during validation\n", encoding="utf-8"
        )

    with pytest.raises(RuntimeError, match="inventory"):
        ReleaseStore(tmp_path / "store").open(
            ReleaseRef("fixture", digest),
            validate=mutate,
        )

    assert validated == ["fixture"]


def test_release_audit_is_reachable_only_through_the_public_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = _write_release(tmp_path / "release")

    assert sigilicon_main(["release", "audit", str(manifest_path)]) == 0
    assert '"ip_name": "fixture"' in capsys.readouterr().out


def test_release_store_rejects_symlinked_namespace_ancestor(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "release-store"
    store_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (store_root / "fixture").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        ReleaseStore(store_root).object_root(
            ReleaseRef("fixture", "d" * 64),
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("fault", ("failed", "nonboolean", "empty", "missing", "export-missing"))
def test_public_audit_rejects_incomplete_maturity(tmp_path: Path, fault: str) -> None:
    manifest_path = _write_release(tmp_path / "release")
    manifest = json.loads(manifest_path.read_text())
    if fault == "failed":
        manifest["maturity"]["checks"][0]["passed"] = False
    elif fault == "nonboolean":
        manifest["maturity"]["checks"][0]["passed"] = 1
    elif fault == "empty":
        manifest["maturity"]["checks"] = []
    elif fault == "missing":
        manifest["maturity"]["missing_items"] = ["fixture:payload"]
    else:
        manifest["exports"][0]["maturity"]["missing_items"] = ["fixture:payload"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RuntimeError, match="maturity"):
        ip_packaging.audit_ip_release_manifest(manifest_path)
