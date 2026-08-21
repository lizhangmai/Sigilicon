from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

import pytest

from sigilicon.flow import (
    FlowEngine,
    FlowExecutionError,
    FlowRegistry,
    RunArtifactReference,
    load_run_artifact_reference,
    run_artifact_reference_payload,
)


OWNER = "Synthesizable-Comparator"
FLOW = "comparator-paper-0p8v"
RUN_ID = "5e75c193dd734b038d3baecb3aac5497"
NODE = "reference-library"
ROLE = "reference-library"
QUALIFIERS = {
    "corner": "tt0p8v25c",
    "nominal_supply_v": 0.8,
    "nominal_temperature_c": 25.0,
    "variant": "paper_0p8v",
}
MEMBER_BYTES = b"durable NDM fixture\n"
MEMBER_DIGEST = sha256(MEMBER_BYTES).hexdigest()
ARTIFACT_BYTES = (
    json.dumps(
        {
            "schema": 1,
            "contract_kind": "artifact-directory-manifest",
            "kind": "library.synopsys-ndm",
            "root": "paper.ndm",
            "members": [{"path": "reflib.ndm", "digest": MEMBER_DIGEST}],
            "qualifiers": QUALIFIERS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    + b"\n"
)
DIGEST = sha256(ARTIFACT_BYTES).hexdigest()
ARTIFACT_RELATIVE = (
    "nodes/reference-library/outputs/reference-library/reference-library.json"
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _paper_run_fixture(tmp_path: Path) -> Path:
    """Materialize the current paper NDM run-record shape without a fake runner."""

    artifact_root = tmp_path / "artifacts"
    run_root = artifact_root / "flows" / OWNER / FLOW / "runs" / RUN_ID
    artifact_path = run_root / ARTIFACT_RELATIVE
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(ARTIFACT_BYTES)
    ndm_member = artifact_path.parent / "paper.ndm/reflib.ndm"
    ndm_member.parent.mkdir()
    ndm_member.write_bytes(MEMBER_BYTES)
    artifact = {
        "kind": "library.synopsys-ndm",
        "path": ARTIFACT_RELATIVE,
        "producer": NODE,
        "qualifiers": QUALIFIERS,
        "digest": DIGEST,
    }
    node_result = {
        "status": "accepted",
        "execution_status": "succeeded",
        "result_status": "valid",
        "policy_status": "accepted",
        "reason": None,
        "artifacts": {ROLE: artifact},
        "facts": {
            "library-check-error-count": 0,
            "library-check-succeeded": True,
            "library-check-warning-count": 20431,
            "tool-execution-completed": True,
        },
    }
    _write_json(
        run_root / "resolved_plan.json",
        {
            "schema": 1,
            "contract_kind": "resolved-flow-plan",
            "owner": OWNER,
            "flow": FLOW,
            "target": "implementation",
            "execution_profile": {
                "owner": OWNER,
                "name": "paper-0p8v-synopsys",
            },
            "topology": ["assets", "reference-library", "implementation"],
            "nodes": [
                {
                    "id": "assets",
                    "source_assets": {
                        "name": "comparator-paper-0p8v-source",
                        "git": {
                            "commit": "b9753f18f7c8c60bffb90fc08c4c628651a658c3",
                            "dirty": False,
                        },
                    },
                }
            ],
            "policies": [],
        },
    )
    _write_json(
        run_root / "preflight.json",
        {
            "schema": 1,
            "contract_kind": "flow-preflight",
            "owner": OWNER,
            "flow": FLOW,
            "target": "implementation",
            "execution_profile": "paper-0p8v-synopsys",
            "status": "ready",
            "checks": [],
        },
    )
    _write_json(
        run_root / "flow_result.json",
        {
            "schema": 1,
            "contract_kind": "flow-result",
            "owner": OWNER,
            "flow": FLOW,
            "target": "implementation",
            "run_id": RUN_ID,
            "status": "accepted",
            "interrupted": False,
            "topology": [NODE],
            "nodes": {NODE: node_result},
        },
    )
    node_root = run_root / "nodes" / NODE
    _write_json(
        node_root / "action_request.json",
        {
            "schema": 1,
            "contract_kind": "action-request",
            "node": NODE,
            "action": "asic.reference-library-construction",
            "adapter": "synopsys-fc",
            "adapter_version": "2",
            "action_config": {},
            "adapter_config": {},
            "execution_environment": {},
            "inputs": {},
            "source_assets": None,
        },
    )
    _write_json(
        node_root / "action_result.json",
        {
            "schema": 1,
            "contract_kind": "action-result",
            "node": NODE,
            "result_status": "valid",
            "execution": {
                "status": "succeeded",
                "exit_code": 0,
                "started_at": "2026-08-22T00:00:00+00:00",
                "finished_at": "2026-08-22T00:00:01+00:00",
                "details": {},
            },
            "artifacts": {ROLE: artifact},
            "facts": node_result["facts"],
            "evidence": [],
            "details": {},
            "error": None,
        },
    )
    _write_json(
        node_root / "policy_receipt.json",
        {
            "schema": 1,
            "contract_kind": "policy-receipt",
            "policy": "reference-library-quality",
            "status": "accepted",
            "checks": [],
        },
    )
    _write_json(
        node_root / "run_manifest.json",
        {
            "schema": 1,
            "contract_kind": "action-run-manifest",
            "node": NODE,
            "managed_paths": [
                path.relative_to(run_root).as_posix()
                for path in sorted(node_root.rglob("*"))
            ],
        },
    )
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
    return artifact_root


def _run_root(artifact_root: Path) -> Path:
    return artifact_root / "flows" / OWNER / FLOW / "runs" / RUN_ID


def _mutate_json(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    _write_json(path, payload)


def _reference(**changes: object) -> RunArtifactReference:
    values = {
        "owner": OWNER,
        "flow_id": FLOW,
        "run_id": RUN_ID,
        "node_id": NODE,
        "role": ROLE,
        "kind": "library.synopsys-ndm",
        "qualifiers": QUALIFIERS,
        "digest": DIGEST,
        "required_policy": "reference-library-quality",
    }
    values.update(changes)
    return RunArtifactReference(**values)


def test_owner_can_resolve_an_exact_accepted_durable_run_artifact(
    tmp_path: Path,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)

    artifact = FlowEngine(FlowRegistry()).resolve_run_artifact(
        artifact_root=artifact_root,
        consumer_owner=OWNER,
        reference=_reference(),
    )

    assert artifact.relative_path == ARTIFACT_RELATIVE
    assert artifact.path.read_bytes() == ARTIFACT_BYTES
    assert artifact.digest == DIGEST


def test_run_artifact_reference_rejects_an_escaping_manifest_path(
    tmp_path: Path,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    manifest_path = (
        artifact_root / "flows" / OWNER / FLOW / "runs" / RUN_ID / "run_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["managed_paths"].append("../../outside")
    _write_json(manifest_path, manifest)

    with pytest.raises(FlowExecutionError, match="unsafe managed path"):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(),
        )


@pytest.mark.parametrize(
    ("name", "reference_changes", "consumer_owner"),
    [
        ("missing-run", {"run_id": "0" * 32}, OWNER),
        ("wrong-owner", {"owner": "Different-Owner"}, "Different-Owner"),
        ("cross-owner", {"owner": OWNER}, "Different-Owner"),
        ("wrong-flow", {"flow_id": "comparator-paper-other"}, OWNER),
        ("wrong-node", {"node_id": "implementation"}, OWNER),
        ("wrong-role", {"role": "different-library"}, OWNER),
        ("wrong-kind", {"kind": "library.liberty"}, OWNER),
        (
            "wrong-qualifier",
            {"qualifiers": {**QUALIFIERS, "corner": "tt0p9v25c"}},
            OWNER,
        ),
        ("wrong-digest", {"digest": "0" * 64}, OWNER),
        ("wrong-policy", {"required_policy": "different-policy"}, OWNER),
    ],
)
def test_run_artifact_reference_rejects_a_wrong_selected_identity(
    tmp_path: Path,
    name: str,
    reference_changes: dict[str, object],
    consumer_owner: str,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)

    with pytest.raises(FlowExecutionError):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=consumer_owner,
            reference=_reference(**reference_changes),
        )


@pytest.mark.parametrize(
    "state",
    ["producer-rejected", "producer-failed", "policy-rejected"],
)
def test_run_artifact_reference_rejects_an_unaccepted_producer(
    tmp_path: Path,
    state: str,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    run_root = _run_root(artifact_root)
    if state == "producer-rejected":
        _mutate_json(
            run_root / "flow_result.json",
            lambda payload: payload["nodes"][NODE].update(
                {"status": "rejected", "policy_status": "rejected"}
            ),
        )
    elif state == "producer-failed":
        _mutate_json(
            run_root / "nodes" / NODE / "action_result.json",
            lambda payload: payload.update({"result_status": "failed"}),
        )
    else:
        _mutate_json(
            run_root / "nodes" / NODE / "policy_receipt.json",
            lambda payload: payload.update({"status": "rejected"}),
        )

    with pytest.raises(FlowExecutionError):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(),
        )


def test_run_artifact_reference_rejects_a_missing_recorded_digest(
    tmp_path: Path,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    run_root = _run_root(artifact_root)
    for record in (
        run_root / "flow_result.json",
        run_root / "nodes" / NODE / "action_result.json",
    ):
        _mutate_json(
            record,
            lambda payload: payload[
                "nodes" if record.name == "flow_result.json" else "artifacts"
            ][NODE if record.name == "flow_result.json" else ROLE][
                "artifacts" if record.name == "flow_result.json" else "digest"
            ].get(ROLE, {}).pop("digest", None)
            if record.name == "flow_result.json"
            else payload["artifacts"][ROLE].pop("digest", None),
        )

    with pytest.raises(FlowExecutionError, match="identity"):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(),
        )


def test_run_artifact_reference_rejects_stale_durable_content(
    tmp_path: Path,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    (_run_root(artifact_root) / ARTIFACT_RELATIVE).write_bytes(
        ARTIFACT_BYTES + b"stale\n"
    )

    with pytest.raises(FlowExecutionError, match="durable digest"):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(),
        )


def test_run_artifact_reference_rejects_a_stale_directory_member(
    tmp_path: Path,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    member = (
        _run_root(artifact_root)
        / Path(ARTIFACT_RELATIVE).parent
        / "paper.ndm/reflib.ndm"
    )
    member.write_bytes(MEMBER_BYTES + b"stale\n")

    with pytest.raises(FlowExecutionError, match="directory member digest"):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(),
        )


def test_ndm_run_artifact_requires_a_directory_manifest(tmp_path: Path) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    run_root = _run_root(artifact_root)
    opaque = b"not an NDM directory manifest\n"
    opaque_digest = sha256(opaque).hexdigest()
    (run_root / ARTIFACT_RELATIVE).write_bytes(opaque)
    for record in (
        run_root / "flow_result.json",
        run_root / "nodes" / NODE / "action_result.json",
    ):
        def mutate(payload: dict[str, Any]) -> None:
            artifacts = (
                payload["nodes"][NODE]["artifacts"]
                if record.name == "flow_result.json"
                else payload["artifacts"]
            )
            artifacts[ROLE]["digest"] = opaque_digest

        _mutate_json(record, mutate)

    with pytest.raises(FlowExecutionError, match="directory manifest"):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(digest=opaque_digest),
        )


@pytest.mark.parametrize("path", ["../../outside.json", "/tmp/outside.json"])
def test_run_artifact_reference_rejects_an_escaping_artifact_record(
    tmp_path: Path,
    path: str,
) -> None:
    artifact_root = _paper_run_fixture(tmp_path)
    run_root = _run_root(artifact_root)
    for record in (
        run_root / "flow_result.json",
        run_root / "nodes" / NODE / "action_result.json",
    ):
        def mutate(payload: dict[str, object]) -> None:
            artifacts = (
                payload["nodes"][NODE]["artifacts"]  # type: ignore[index]
                if record.name == "flow_result.json"
                else payload["artifacts"]
            )
            artifacts[ROLE]["path"] = path  # type: ignore[index]

        _mutate_json(record, mutate)

    with pytest.raises(FlowExecutionError, match="path is unsafe"):
        FlowEngine(FlowRegistry()).resolve_run_artifact(
            artifact_root=artifact_root,
            consumer_owner=OWNER,
            reference=_reference(),
        )


def test_run_artifact_reference_has_one_portable_current_json_contract() -> None:
    payload = run_artifact_reference_payload(_reference())

    assert payload == {
        "schema": 1,
        "contract_kind": "run-artifact-reference",
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
    assert load_run_artifact_reference(payload) == _reference()
    encoded = json.dumps(payload)
    assert "/tmp" not in encoded
    assert all(token not in encoded for token in ("fingerprint", "cache_key", "hash"))
