from __future__ import annotations

import json
from pathlib import Path
from pathlib import PurePosixPath
import subprocess

import pytest

from sigilicon.cli.main import main as sigilicon_cli_main
from sigilicon.workflows.ip_packaging import (
    IpReleaseError,
    audit_ip_release,
    promote_ip_release,
)
from sigilicon.domain.ip_integration import LockedIpRelease
from sigilicon.workflows.ip_integration import resolve_locked_ip_release
from test_run_artifact_reference import (
    DIGEST,
    FLOW,
    NODE,
    OWNER,
    QUALIFIERS,
    ROLE,
    RUN_ID,
    _run_fixture,
    _run_root,
    _write_json,
)


IMPLEMENTATION = "implementation"


def _refresh_run_manifest(run_root: Path) -> None:
    _write_json(
        run_root / "run_manifest.json",
        {
            "schema": 1,
            "contract_kind": "flow-run-manifest",
            "owner": OWNER,
            "flow": FLOW,
            "target": "implementation",
            "run_id": RUN_ID,
            "managed_paths": [
                path.relative_to(run_root).as_posix()
                for path in sorted(run_root.rglob("*"))
                if path != run_root / "run_manifest.json"
            ],
        },
    )


def _promotion_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    artifact_root = _run_fixture(tmp_path)
    run_root = _run_root(artifact_root)

    flow_result_path = run_root / "flow_result.json"
    flow_result = json.loads(flow_result_path.read_text(encoding="utf-8"))
    facts = {
        "tool-execution-completed": True,
        "design-check-error-count": 0,
        "open-net-count": 0,
        "route-drc-violation-count": 0,
        "antenna-check-active": True,
        "antenna-violation-count": 0,
    }
    flow_result["nodes"][IMPLEMENTATION] = {
        "status": "accepted",
        "execution_status": "succeeded",
        "result_status": "valid",
        "policy_status": "accepted",
        "reason": None,
        "artifacts": {},
        "facts": facts,
    }
    _write_json(flow_result_path, flow_result)

    implementation_root = run_root / "nodes" / IMPLEMENTATION
    _write_json(
        implementation_root / "action_result.json",
        {
            "schema": 1,
            "contract_kind": "action-result",
            "node": IMPLEMENTATION,
            "result_status": "valid",
            "execution": {"status": "succeeded", "exit_code": 0},
            "artifacts": {},
            "facts": facts,
            "evidence": [],
            "details": {},
            "error": None,
        },
    )
    _write_json(
        implementation_root / "policy_receipt.json",
        {
            "schema": 1,
            "contract_kind": "policy-receipt",
            "policy": "implementation-regression",
            "status": "accepted",
            "checks": [
                {
                    "check_id": "tool-completed",
                    "status": "accepted",
                    "actual": True,
                    "expected": True,
                }
            ],
        },
    )

    plan_path = run_root / "resolved_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["policies"] = [
        {
            "policy_id": "physical-completion-readiness",
            "checks": [
                {
                    "check_id": "antenna-check-active",
                    "fact": "antenna-check-active",
                    "operator": "equals",
                    "expected": True,
                },
                {
                    "check_id": "no-antenna-violations",
                    "fact": "antenna-violation-count",
                    "operator": "at_most",
                    "expected": 0,
                },
            ],
        }
    ]
    _write_json(plan_path, plan)
    _write_json(
        implementation_root / "run_manifest.json",
        {
            "schema": 1,
            "contract_kind": "action-run-manifest",
            "node": IMPLEMENTATION,
            "managed_paths": [
                path.relative_to(run_root).as_posix()
                for path in sorted(implementation_root.rglob("*"))
                if path != implementation_root / "run_manifest.json"
            ],
        },
    )
    _refresh_run_manifest(run_root)

    owner = tmp_path / "ip/Fixture_Block"
    configs = owner / "configs"
    configs.mkdir(parents=True)
    (owner / "component.toml").write_text(
        """schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "Fixture-Block"
name = "fixture-block"
kind = "rtl-ip"
[filesets]
specification = ["ip/Fixture_Block/configs/interface.toml"]
""",
        encoding="utf-8",
    )
    (configs / "interface.toml").write_text(
        """schema = 1
contract_kind = "ip-interface"
path_scope = "owner"
owner = "Fixture-Block"
[module]
name = "fixture_block_variant_a"
""",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture source"], cwd=tmp_path, check=True
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["nodes"][0]["source_assets"]["git"]["commit"] = source_commit
    _write_json(plan_path, plan)
    _refresh_run_manifest(run_root)

    contract = configs / "promotion.toml"
    contract.write_text(
        f"""schema = 1
contract_kind = "ip-promotion"
path_scope = "owner"
owner = "Fixture-Block"

name = "fixture-block"
producer = "ip/Fixture_Block"
component = "component.toml"
export = "fixture-export"
maturity = "development"

[source]
commit = "{source_commit}"
dirty = false

[interface]
contract = "configs/interface.toml"
logical = "fixture_block_variant_a:rtl"
physical = "fixture_block_variant_a:routed"

[conclusions]
implementation_regression = true
physical_completion_readiness = true
qualification = false
signoff = false

[[artifacts]]
schema = 1
contract_kind = "run-artifact-reference"
owner = "{OWNER}"
flow = "{FLOW}"
run_id = "{RUN_ID}"
node = "{NODE}"
role = "{ROLE}"
kind = "library.synopsys-ndm"
digest = "{DIGEST}"
required_policy = "reference-library-quality"
qualifiers = {{ corner = "nominal_a", nominal_supply_v = 0.8, nominal_temperature_c = 25.0, variant = "variant_a" }}

[[evidence]]
node = "implementation"
role = "implementation-regression"
policy = "implementation-regression"
evidence_role = "regression"
evaluation = "producer"

[[evidence]]
node = "implementation"
role = "physical-completion-readiness"
policy = "physical-completion-readiness"
evidence_role = "readiness"
evaluation = "run-policy"

[[boundaries]]
kind = "integration-antenna-coverage"
status = "open"
summary = "Top input ports lack gate-area context; block readiness is not antenna signoff."
subjects = ["vin_p", "vin_n", "clk_cmp", "cal_en", "rst_n"]
""",
        encoding="utf-8",
    )
    return contract, artifact_root, source_commit


def test_promotion_builds_one_immutable_release_from_exact_run_evidence(
    tmp_path: Path,
) -> None:
    contract, artifact_root, source_commit = _promotion_fixture(tmp_path)

    first = promote_ip_release(
        contract,
        project_root=tmp_path,
        artifact_root=artifact_root,
    )
    second = promote_ip_release(
        contract,
        project_root=tmp_path,
        artifact_root=artifact_root,
    )

    assert first == second
    assert first["source"] == {"commit": source_commit, "dirty": False}
    assert first["maturity"] == {"level": "development"}
    assert first["conclusions"] == {
        "implementation_regression": True,
        "physical_completion_readiness": True,
        "qualification": False,
        "signoff": False,
    }
    assert first["artifacts"][0]["reference"] == {
        "owner": OWNER,
        "flow": FLOW,
        "run_id": RUN_ID,
        "node": NODE,
        "role": ROLE,
        "kind": "library.synopsys-ndm",
        "qualifiers": QUALIFIERS,
        "digest": DIGEST,
        "required_policy": "reference-library-quality",
    }
    assert [item["policy"] for item in first["evidence"]] == [
        "implementation-regression",
        "physical-completion-readiness",
    ]
    assert all(item["status"] == "accepted" for item in first["evidence"])
    assert all(len(item["digest"]) == 64 for item in first["evidence"])
    assert first["boundaries"][0]["subjects"] == [
        "vin_p",
        "vin_n",
        "clk_cmp",
        "cal_en",
        "rst_n",
    ]
    encoded = json.dumps(first)
    assert str(tmp_path) not in encoded
    assert not any(token in encoded for token in ("cache_key", "source_fingerprint"))
    manifest_relative = PurePosixPath(
        "ip"
    ) / "fixture-block" / first["release_id"] / "manifest.json"
    manifest_path, locked = resolve_locked_ip_release(
        artifact_root=artifact_root,
        pinned=LockedIpRelease(
            name="fixture-block",
            release_id=first["release_id"],
            manifest=manifest_relative,
            maturity="development",
        ),
    )
    assert manifest_path == artifact_root / Path(manifest_relative)
    assert locked["release_id"] == first["release_id"]


def test_public_cli_promotes_then_read_only_audits_the_same_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract, artifact_root, _ = _promotion_fixture(tmp_path)
    catalog = tmp_path / "catalogs" / "ip.toml"
    catalog.write_text(
        """schema = 1
contract_kind = "ip-catalog"
path_scope = "repository"
owner = "repository"

[targets.fixture-target]
contract = "ip/Fixture_Block/configs/promotion.toml"

[components]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert (
        sigilicon_cli_main(
            [
                "ip",
                "promote",
                "fixture-target",
                "--artifact-root",
                str(artifact_root),
                "--json",
            ]
        )
        == 0
    )
    promoted = json.loads(capsys.readouterr().out)

    assert (
        sigilicon_cli_main(
            [
                "ip",
                "audit",
                "fixture-target",
                "--artifact-root",
                str(artifact_root),
                "--json",
            ]
        )
        == 0
    )
    audited = json.loads(capsys.readouterr().out)
    assert audited == promoted
    assert audited["conclusions"] == {
        "implementation_regression": True,
        "physical_completion_readiness": True,
        "qualification": False,
        "signoff": False,
    }
    assert contract.is_file()


def test_promotion_audit_never_creates_a_missing_release(tmp_path: Path) -> None:
    contract, artifact_root, _ = _promotion_fixture(tmp_path)

    with pytest.raises(FileNotFoundError, match="has not been built"):
        audit_ip_release(
            contract,
            project_root=tmp_path,
            artifact_root=artifact_root,
        )

    assert not (artifact_root / "ip").exists()


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("dirty-source", "dirty source"),
        ("producer-rejected", "not accepted"),
        ("producer-action-failed", "not accepted"),
        ("missing-policy", "required policy"),
        ("wrong-digest", "digest"),
        ("qualification-masquerade", "qualification"),
        ("signoff-masquerade", "signoff"),
    ],
)
def test_promotion_rejects_invalid_or_overclaimed_evidence(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    contract, artifact_root, _ = _promotion_fixture(tmp_path)
    run_root = _run_root(artifact_root)
    if mutation == "dirty-source":
        plan_path = run_root / "resolved_plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["nodes"][0]["source_assets"]["git"]["dirty"] = True
        _write_json(plan_path, plan)
        _refresh_run_manifest(run_root)
    elif mutation == "producer-rejected":
        result_path = run_root / "flow_result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["nodes"][IMPLEMENTATION]["status"] = "rejected"
        _write_json(result_path, result)
        _refresh_run_manifest(run_root)
    elif mutation == "producer-action-failed":
        action_path = run_root / "nodes" / IMPLEMENTATION / "action_result.json"
        action = json.loads(action_path.read_text(encoding="utf-8"))
        action["result_status"] = "failed"
        action["execution"]["status"] = "failed"
        _write_json(action_path, action)
    elif mutation == "missing-policy":
        text = contract.read_text(encoding="utf-8").replace(
            'policy = "physical-completion-readiness"',
            'policy = "missing-readiness-policy"',
        )
        contract.write_text(text, encoding="utf-8")
    elif mutation == "wrong-digest":
        contract.write_text(
            contract.read_text(encoding="utf-8").replace(DIGEST, "0" * 64),
            encoding="utf-8",
        )
    elif mutation == "qualification-masquerade":
        contract.write_text(
            contract.read_text(encoding="utf-8").replace(
                "qualification = false", "qualification = true"
            ),
            encoding="utf-8",
        )
    else:
        contract.write_text(
            contract.read_text(encoding="utf-8").replace(
                "signoff = false", "signoff = true"
            ),
            encoding="utf-8",
        )

    with pytest.raises(IpReleaseError, match=match):
        promote_ip_release(
            contract,
            project_root=tmp_path,
            artifact_root=artifact_root,
        )


def test_promotion_rejects_a_missing_required_interface(tmp_path: Path) -> None:
    contract, artifact_root, _ = _promotion_fixture(tmp_path)
    (contract.parent / "interface.toml").unlink()

    with pytest.raises((FileNotFoundError, IpReleaseError), match="interface"):
        promote_ip_release(
            contract,
            project_root=tmp_path,
            artifact_root=artifact_root,
        )


def test_release_lock_rejects_a_manifest_that_overclaims_qualification(
    tmp_path: Path,
) -> None:
    contract, artifact_root, _ = _promotion_fixture(tmp_path)
    release = promote_ip_release(
        contract,
        project_root=tmp_path,
        artifact_root=artifact_root,
    )
    manifest_relative = (
        PurePosixPath("ip")
        / "fixture-block"
        / release["release_id"]
        / "manifest.json"
    )
    manifest_path = artifact_root / Path(manifest_relative)
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["conclusions"]["qualification"] = True
    _write_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="qualification"):
        resolve_locked_ip_release(
            artifact_root=artifact_root,
            pinned=LockedIpRelease(
                name="fixture-block",
                release_id=release["release_id"],
                manifest=manifest_relative,
                maturity="development",
            ),
        )
