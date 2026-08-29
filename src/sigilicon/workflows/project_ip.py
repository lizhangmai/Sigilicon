"""Project-bound entrypoint for custom-IP release and integration workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigilicon.domain.repository import Project
from sigilicon.workflows.ip_integration import (
    check_ip_integration,
    ip_catalog_contract_path,
    plan_ip_integration,
)
from sigilicon.workflows.ip_packaging import (
    audit_ip_release,
    build_ip_release,
    plan_ip_release,
    publish_ip_release,
)


@dataclass(frozen=True)
class ProjectIpWorkflow:
    """Own IP catalog selection, release operations, and composite integration."""

    _project: Project

    @classmethod
    def from_file(cls, project_contract: Path | str) -> "ProjectIpWorkflow":
        return cls(Project.from_file(project_contract))

    @property
    def project_root(self) -> Path:
        return self._project.project_root

    @property
    def artifact_root(self) -> Path:
        return self._project.artifact_root

    def contract(self, target: str, *, section: str = "targets") -> Path:
        return ip_catalog_contract_path(
            None,
            target,
            project=self._project,
            section=section,
        )

    def plan(
        self, contract_path: Path, *, maturity: str | None = None
    ) -> dict[str, Any]:
        return plan_ip_release(
            contract_path,
            project=self._project,
            maturity=maturity,
        )

    def build(
        self, contract_path: Path, *, maturity: str | None = None
    ) -> dict[str, Any]:
        return build_ip_release(
            contract_path,
            project=self._project,
            maturity=maturity,
        )

    def audit(
        self,
        contract_path: Path,
        *,
        maturity: str | None = None,
        artifact_root: Path | None = None,
    ) -> dict[str, Any]:
        return audit_ip_release(
            contract_path,
            project=self._project,
            artifact_root=artifact_root,
            maturity=maturity,
        )

    def publish(
        self, contract_path: Path, *, maturity: str | None = None
    ) -> dict[str, Any]:
        return publish_ip_release(
            contract_path,
            project=self._project,
            maturity=maturity,
        )

    def plan_integration(self, contract_path: Path) -> dict[str, Any]:
        return plan_ip_integration(contract_path, project=self._project)

    def check_integration(
        self,
        contract_path: Path,
        *,
        variant_name: str,
        fileset_name: str | None = None,
    ) -> dict[str, Any]:
        return check_ip_integration(
            contract_path,
            project=self._project,
            variant_name=variant_name,
            fileset_name=fileset_name,
        )
