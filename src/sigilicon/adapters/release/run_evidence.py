"""Bind release attestations to audited execution and a supported typed conclusion."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping

from sigilicon.canonical import canonical_digest
from sigilicon.execution.artifact_reference import ArtifactReference
from sigilicon.execution.materialization import MaterializationPlan, validate_materialization_record


def execution_from_run(plan: MaterializationPlan, reference: ArtifactReference) -> dict:
    """Create portable evidence only after RunStore has verified the closed run."""

    artifact, = plan.artifacts(reference)
    payload = artifact.read_text()
    record = {"kind": "managed-run", "proof": plan.record,
              "reference": reference.record, "payload": payload}
    validate_execution(record)
    return record


@dataclass(frozen=True)
class VerifiedExecution:
    """The specific claim proven by one audited adapter invocation."""

    check: str
    owner: str
    subject: str
    variant: str | None
    condition: Mapping
    coverage: frozenset[str]
    inputs: frozenset[tuple[int, str]]
    outputs: frozenset[tuple[int, str]]


def validate_execution(record: Mapping) -> VerifiedExecution:
    """Validate execution, artifact identity and adapter-specific conclusion together."""

    if not isinstance(record, Mapping) or set(record) != {"kind", "proof", "reference", "payload"} or record["kind"] != "managed-run":
        raise ValueError("receipt requires managed execution evidence")
    proof = record["proof"]
    if not isinstance(proof, Mapping) or proof.get("schema") != 1 or proof.get("contract_kind") != "run-materialization":
        raise ValueError("invalid run materialization evidence")
    validate_materialization_record(proof)
    plan, result = proof.get("plan"), proof.get("result")
    if not isinstance(plan, Mapping) or not isinstance(result, Mapping):
        raise ValueError("receipt has no execution plan and result")
    if (plan.get("schema") != 18 or not isinstance(plan.get("software"), Mapping)
            or canonical_digest(plan) != result.get("plan_identity") or result.get("status") != "succeeded"
            or any(result.get(key) != plan.get(key) for key in ("owner", "operation", "variant"))):
        raise ValueError("receipt execution plan/result identity drift")
    reference = ArtifactReference.from_record(record["reference"])
    steps = result.get("steps", [])
    if not steps or any(step.get("status") != "succeeded" for step in steps):
        raise ValueError("receipt run contains an incomplete step")
    producer = next((step for step in steps if step.get("id") == reference.step), None)
    planned = next((step for step in plan.get("steps", []) if step.get("id") == reference.step), None)
    if producer is None or planned is None or producer.get("uses") != planned.get("uses"):
        raise ValueError("receipt producer disagrees with its execution plan")
    if (planned.get("evidence") or {}).get("role") not in {"qualification", "signoff"}:
        raise ValueError("diagnostic or regression execution cannot supply signoff receipts")
    prefix = f"outputs/{reference.step}/"
    artifacts = proof.get("artifacts", [])
    selected = [item for item in artifacts if item.get("step") == reference.step
                and item.get("role") == reference.role and item.get("kind") == reference.kind
                and (reference.path is None or item.get("path") == prefix + reference.path)]
    if reference.cardinality != "one" or len(selected) != 1:
        raise ValueError("receipt must select exactly one typed evidence artifact")
    artifact = selected[0]
    if not any(all(item.get(key) == artifact.get(key) for key in ("path", "role", "kind"))
               for item in producer.get("artifacts", [])):
        raise ValueError("receipt artifact is not registered by its producer")
    payload = record["payload"]
    if not isinstance(payload, str) or (len(payload.encode()), hashlib.sha256(payload.encode()).hexdigest()) != (artifact.get("size"), artifact.get("sha256")):
        raise ValueError("receipt evidence payload identity drift")
    evidence = json.loads(payload)
    if not isinstance(evidence, Mapping):
        raise ValueError("receipt evidence must be an object")
    adapter = producer["uses"]
    if adapter == "mentor.calibre" and reference.kind == "evidence.physical-verification":
        completion = evidence.get("completion", {})
        layout = evidence.get("layout", {})
        if layout.get("plan_identity") != result["plan_identity"] or layout.get("owner") != result["owner"]:
            raise ValueError("receipt checked layout belongs to a different execution")
        if (evidence.get("status") != "clean" or completion.get("adapter") != adapter
                or completion.get("executed") is not True or completion.get("report_parsed") is not True
                or type(completion.get("exit_code")) is not int or completion["exit_code"] != 0
                or evidence.get("violations", []) or evidence.get("mismatches", [])):
            raise ValueError("receipt physical verification is not proven clean")
    elif adapter in {"synopsys.dc", "synopsys.fc"} and reference.kind == "evidence.tool-verdict":
        checks = evidence.get("checks")
        if (evidence.get("passed") is not True or not isinstance(checks, Mapping) or not checks
                or any(value is not True for value in checks.values())
                or any(evidence.get(key) != result.get(key) for key in ("run_id", "plan_identity", "owner"))
                or evidence.get("step_id") != reference.step):
            raise ValueError("receipt tool verdict is not a proven execution conclusion")
    elif adapter == "cadence.spectre" and reference.kind == "evidence.measurement":
        action = planned["action"]
        if (action.get("kind") != "spectre-measurement"
                or any(evidence.get(key) != result.get(key) for key in ("run_id", "plan_identity", "owner"))
                or evidence.get("step") != reference.step or evidence.get("subject") != action["top"]
                or evidence.get("measurements", {}).get("passed") is not True):
            raise ValueError("receipt measurement is not a proven execution conclusion")
    else:
        raise ValueError("adapter cannot supply release signoff evidence")
    action = planned["action"]
    sources = {item["path"]: item for item in plan["sources"]}
    def identities(rows):
        return frozenset((item["size"], item["sha256"]) for item in rows)
    def checked_digest(value):
        digest = value.removeprefix("sha256-")
        rows = [item for item in (*sources.values(), *artifacts) if item["sha256"] == digest]
        if not rows:
            raise ValueError("checked input is absent from execution evidence")
        return identities(rows)
    if adapter == "cadence.spectre":
        inputs = identities(sources[path] for path in (action["circuit"], action["spec"], *action["inputs"]))
        coverage = frozenset(key for key, value in evidence["measurements"].get("criteria", {}).items() if value is True)
        outputs = identities(item for item in artifacts if item["step"] == reference.step
                             and item["kind"] in {"raw.cadence-spectre", "table.measurement"})
        return VerifiedExecution("measurement", result["owner"], action["top"], plan["variant"],
                                 evidence["condition"], coverage, inputs, outputs)
    if adapter == "mentor.calibre":
        check = action["check"]
        if check not in {"drc", "lvs"} or (check == "lvs") != ("source" in evidence):
            raise ValueError("physical evidence check disagrees with its action")
        if layout["name"] != action["cell"] or action["owner"] != result["owner"]:
            raise ValueError("checked subject disagrees with its action")
        inputs = checked_digest(layout["artifact_identity"])
        if check == "lvs":
            source = evidence["source"]
            if source["name"] != layout["name"] or source["owner"] != result["owner"]:
                raise ValueError("LVS source subject disagrees with checked layout")
            inputs |= checked_digest(source["artifact_identity"])
        outputs = identities(item for item in artifacts if item["step"] == reference.step
                             and item["kind"] == "netlist.cdl")
        return VerifiedExecution(check, result["owner"], layout["name"], plan["variant"], {},
                                 frozenset(), inputs, outputs)
    check = "synthesis" if adapter == "synopsys.dc" else "physical-implementation"
    variant = action["variant"] if adapter == "synopsys.dc" else action["invocation"]["variant"]
    if (evidence.get("stage") != check or evidence.get("variant") != variant
            or evidence.get("corner") != action["corner"]):
        raise ValueError("tool verdict applicability disagrees with its action")
    if adapter == "synopsys.dc":
        inputs = identities(sources[path] for path in (*action["hdl"]["sources"], action["constraints"]))
        for library in action["libraries"]:
            inputs |= identities(item for item in artifacts if item["step"] == library["step"]
                                 and item["role"] == library["role"] and item["kind"] == library["kind"]
                                 and (library["path"] is None or item["path"] ==
                                      f'outputs/{library["step"]}/{library["role"]}/{library["path"]}'))
    else:
        inputs = identities(item for item in artifacts
            if (item["step"] == action["synthesis_step"] and item["role"] in {"mapped-netlist", "mapped-constraints"})
            or (item["step"] == action["reference_step"] and item["role"] == "reference-library"))
    outputs = identities(item for item in artifacts if item["step"] == reference.step
                         and not item["kind"].startswith(("evidence.", "log.", "report.")))
    return VerifiedExecution(check, result["owner"], action["hdl"]["top"] if adapter == "synopsys.dc" else action["top"], variant,
                             {"corner": action["corner"]}, frozenset(checks), inputs, outputs)
