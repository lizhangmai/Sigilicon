"""Source identities shared by domain contracts and managed execution."""

from __future__ import annotations

from dataclasses import dataclass

from sigilicon.paths import validate_artifact_component
from sigilicon.contracts import require_text


@dataclass(frozen=True)
class SourceReference:
    """A source identity qualified by its declaring component."""

    component: str
    source: str | None = None

    def __post_init__(self) -> None:
        validate_artifact_component(self.component, "source component")
        if self.source is not None:
            require_text(self.source, "component source identity")

    @property
    def record(self) -> dict[str, str | None]:
        return {"component": self.component, "source": self.source}


@dataclass(frozen=True)
class ComponentFilesetReference:
    """One explicitly qualified fileset selected by an owner operation."""

    component: str
    fileset: str

    def __post_init__(self) -> None:
        validate_artifact_component(self.component, "fileset component")
        validate_artifact_component(self.fileset, "fileset name")
