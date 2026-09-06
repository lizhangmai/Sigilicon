"""Format and cardinality contracts for artifacts exchanged between steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from sigilicon.contracts import ContractReader, require_relative_path
from sigilicon.execution._values import ContractError, _identifier, adapter_identity


@dataclass(frozen=True)
class ArtifactReference:
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
