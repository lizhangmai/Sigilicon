"""Verified materialization of artifacts from a closed successful run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sigilicon.artifacts import copy_immutable_file
from sigilicon.canonical import canonical_digest
from sigilicon.contracts import freeze_toml_document, thaw_toml_document
from sigilicon.execution.artifact_reference import ArtifactReference
from sigilicon.execution._result import Artifact, RunResult
from sigilicon.execution._values import ContractError
from sigilicon.contracts import require_relative_path
from sigilicon.paths import validate_artifact_id


def validate_materialization_record(proof: Mapping) -> None:
    """Check the portable plan/result/artifact closure without current project source."""

    try:
        if proof["schema"] != 1 or proof["contract_kind"] != "run-materialization":
            raise ValueError("invalid materialization schema")
        validate_artifact_id(proof["manifest_identity"], "run manifest identity")
        plan, result = proof["plan"], proof["result"]
        if (plan["schema"] != 18 or result["schema"] != 3
                or plan["contract_kind"] != "execution-plan" or result["contract_kind"] != "run-result"
                or canonical_digest(plan) != result["plan_identity"]
                or result["status"] != "succeeded"
                or any(plan[key] != result[key] for key in ("owner", "operation", "variant"))
                or set(plan["software"]) != {"python", "packages", "sigilicon_sources"}):
            raise ValueError("materialization execution identity drift")
        expected = {}
        steps = {step["id"]: step for step in plan["steps"]}
        if len(steps) != len(plan["steps"]) or len(result["steps"]) != len(steps):
            raise ValueError("materialization execution step closure drift")
        for step in result["steps"]:
            if step["status"] != "succeeded" or step["uses"] != steps.pop(step["id"])["uses"]:
                raise ValueError("materialization requires successful declared steps")
            for item in step["artifacts"]:
                path = require_relative_path(item["path"], "materialization artifact path")
                if path.parts[:2] != ("outputs", step["id"]) or path.as_posix() in expected:
                    raise ValueError("materialization artifact path is outside its producer")
                expected[path.as_posix()] = (step["id"], item["role"], item["kind"])
        actual = {}
        for item in proof["artifacts"]:
            digest = item["sha256"]
            if type(item["size"]) is not int or item["size"] < 0 or not isinstance(digest, str) or len(digest) != 64:
                raise ValueError("materialization artifact identity is invalid")
            if item["path"] in actual:
                raise ValueError("duplicate materialization artifact")
            actual[item["path"]] = (item["step"], item["role"], item["kind"])
        if actual != expected:
            raise ValueError("materialization artifact inventory drift")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed materialization evidence") from exc


@dataclass(frozen=True)
class MaterializationPlan:
    result: RunResult
    execution_plan: Mapping
    manifest_identity: str

    def __post_init__(self) -> None:
        if self.result.status != "succeeded":
            raise ContractError("materialization requires a successful closed run")
        document = freeze_toml_document(self.execution_plan)
        if canonical_digest(thaw_toml_document(document)) != self.result.plan_identity:
            raise ContractError("materialization plan identity disagrees with its run")
        object.__setattr__(self, "execution_plan", document)

    def artifacts(self, reference: ArtifactReference) -> tuple[Artifact, ...]:
        outcome = next((item for item in self.result.outcomes if item.step == reference.step), None)
        if outcome is None:
            raise ContractError("artifact producer is absent from the closed run")
        root = self.result.run_root / "outputs" / reference.step
        selected = tuple(item for item in outcome.result.artifacts
                         if item.role == reference.role and
                         (reference.path is None or item.path == root / reference.path))
        if (not selected or (reference.cardinality == "one" and len(selected) != 1)
                or any(item.kind != reference.kind for item in selected)):
            raise ContractError("stored artifact format or cardinality disagrees with its reference")
        return selected

    def materialize(self, reference: ArtifactReference, destination: Path) -> Artifact:
        if reference.cardinality != "one":
            raise ContractError("file materialization requires singular cardinality")
        artifact, = self.artifacts(reference)
        copy_immutable_file(artifact.path, destination, expected_size=artifact.size,
                            expected_sha256=artifact.sha256)
        return Artifact(artifact.role, artifact.kind, destination, artifact.size, artifact.sha256)

    @property
    def record(self) -> dict:
        return {"schema": 1, "contract_kind": "run-materialization",
                "manifest_identity": self.manifest_identity,
                "plan": thaw_toml_document(self.execution_plan), "result": self.result.record,
                "artifacts": [{"step": outcome.step, "role": artifact.role, "kind": artifact.kind,
                               "path": artifact.path.relative_to(self.result.run_root).as_posix(),
                               "size": artifact.size, "sha256": artifact.sha256}
                              for outcome in self.result.outcomes for artifact in outcome.result.artifacts]}
