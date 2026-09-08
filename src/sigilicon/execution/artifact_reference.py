"""Format and cardinality contracts for artifacts exchanged between steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from sigilicon.contracts import ContractReader, require_relative_path
from sigilicon.execution._values import ContractError, _identifier, adapter_identity


@dataclass(frozen=True)
class ArtifactReference:
    """Select typed outputs; an optional path is relative to the producer step."""

    step: str
    role: str
    kind: str
    cardinality: Literal["one", "many"] = "one"
    path: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.step, "artifact producer step")
        _identifier(self.role, "artifact role")
        adapter_identity(self.kind)
        if self.path is not None:
            require_relative_path(self.path, "artifact member path")
        if self.cardinality not in {"one", "many"}:
            raise ContractError("artifact cardinality must be one or many")

    @classmethod
    def from_record(cls, raw: Mapping) -> ArtifactReference:
        reader = ContractReader(raw, "artifact reference")
        result = cls(reader.text("step"), reader.text("role"), reader.text("kind"),
                     reader.text("cardinality", "one"), reader.text("path", None))
        reader.finish()
        return result

    @property
    def record(self) -> dict[str, str | None]:
        return {"step": self.step, "role": self.role, "kind": self.kind,
                "cardinality": self.cardinality, "path": self.path}


@dataclass(frozen=True)
class ArtifactProduct:
    """One guaranteed artifact or an explicitly variable group from a producer."""
    role: str
    kind: str
    cardinality: Literal["one", "many"] = "one"
    path: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        ArtifactReference("producer", self.role, self.kind, self.cardinality, self.path)
        if type(self.required) is not bool:
            raise ContractError("artifact product required must be boolean")


@dataclass(frozen=True)
class StepContract:
    consumes: tuple[ArtifactReference, ...] = ()
    produces: tuple[ArtifactProduct, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.consumes, tuple) or any(not isinstance(item, ArtifactReference) for item in self.consumes):
            raise ContractError("step consumes must be typed artifact references")
        if not isinstance(self.produces, tuple) or any(not isinstance(item, ArtifactProduct) for item in self.produces):
            raise ContractError("step produces must be typed artifact products")


def validate_step_contracts(steps, contracts: Mapping[str, StepContract]) -> None:
    for step in steps:
        for reference in contracts[step.id].consumes:
            if reference.step not in step.needs:
                raise ContractError(f"step {step.id}: artifact producer must be a declared dependency")
            selected = [item for item in contracts[reference.step].produces
                        if item.role == reference.role and (reference.path is None or item.path == reference.path)]
            if not selected:
                raise ContractError(f"step {step.id}: producer {reference.step} declares no artifact for role {reference.role}")
            if any(item.kind != reference.kind for item in selected):
                raise ContractError(f"step {step.id}: artifact kind mismatch for {reference.step}/{reference.role}; expected {reference.kind}")
            if not any(item.required for item in selected):
                raise ContractError(f"step {step.id}: artifact input is not guaranteed by its producer")
            if reference.cardinality == "one" and (len(selected) != 1 or selected[0].cardinality != "one"):
                raise ContractError(f"step {step.id}: artifact cardinality is not one for {reference.step}/{reference.role}")
