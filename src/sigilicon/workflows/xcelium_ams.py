"""Managed Xcelium AMS execution against one locked native-OA release."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping

from sigilicon.domain.ip_integration import (
    OaNativeReleaseInterfaceReference,
    load_ip_integration_contract,
)
from sigilicon.domain.platform import PdkConfig, SimulationModelSet, load_platform
from sigilicon.domain.repository import Project
from sigilicon.domain.verification_cell import VerificationCellSpec, load_verification_cell
from sigilicon.external_tools import (
    run_process_group_capture,
    xrun_env,
)
from sigilicon.workflows.run_artifacts import RunArtifacts
from sigilicon.workflows.ip_integration import check_ip_integration
from sigilicon.workflows.xcelium import (
    XceliumCellPlan,
    XceliumExecution,
    _execute_xcelium,
    resolve_xcelium_contract,
    snapshot_verification_sources,
)


_AMS_HDL_SUFFIXES = frozenset({".sv", ".v", ".vams", ".va"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _spectre_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class XceliumAmsCellPlan(XceliumCellPlan):
    """Resolved HDL, release, platform, and native-cell identity for one run."""

    circuit_netlist: Path
    circuit_sha256: str
    native_cell: str
    platform: PdkConfig
    model_set: SimulationModelSet
    model_sha256: Mapping[Path, str]
    integration_check: Mapping[str, Any]

    def render_ams_control(
        self,
        *,
        circuit_netlist: Path | None = None,
        model_file: Path | None = None,
    ) -> str:
        ams = self.spec.ams
        assert ams is not None
        section = self.model_set.single_section
        circuit = self.circuit_netlist if circuit_netlist is None else circuit_netlist
        model = self.model_set.file if model_file is None else model_file
        return (
            "simulator lang=spectre\n"
            f'include "{_spectre_path(circuit)}"\n'
            f'include "{_spectre_path(model)}" section={section}\n'
            f"tran tran stop={ams.transient_stop}\n\n"
            "amsd {\n"
            f"    portmap subckt={self.native_cell} autobus=yes\n"
            f"    config cell={self.native_cell} use=spice\n"
            f"    ie vsup={ams.ie_voltage:g}\n"
            "}\n"
        )

    def as_dict(self) -> dict[str, object]:
        root = self.spec.project_root
        ams = self.spec.ams
        assert ams is not None
        return {
            **self.spec.as_dict(),
            "contract": self.contract.relative_to(root).as_posix(),
            "sources": [
                path.relative_to(root).as_posix() for path in self.sources
            ],
            "native_release": {
                "dependency": ams.dependency,
                "role": ams.circuit_role,
                "cell": self.native_cell,
                "circuit_netlist": self.circuit_netlist.name,
                "circuit_sha256": self.circuit_sha256,
                "integration_check": dict(self.integration_check),
            },
            "platform_model": {
                "platform": self.platform.key,
                "model_set": self.model_set.name,
                "file": self.model_set.file.name,
                "section": self.model_set.single_section,
                "support_files": [
                    path.name for path in self.model_set.support_files
                ],
                "sha256": {
                    path.name: digest for path, digest in self.model_sha256.items()
                },
            },
            "command_template": list(self.command_template),
            "evidence_role": "migration_regression",
            "product_qualification_conclusion": False,
        }


def _native_dependency_cell(
    spec: VerificationCellSpec,
) -> str:
    ams = spec.ams
    assert ams is not None
    integration = load_ip_integration_contract(
        ams.integration_contract,
        project=spec.project,
    )
    matches = [
        dependency
        for dependency in integration.release_dependencies
        if dependency.name == ams.dependency
    ]
    if len(matches) != 1 or matches[0].release is None:
        raise ValueError(
            f"Xcelium AMS dependency is not one released dependency: {ams.dependency}"
        )
    interface = matches[0].release.interface
    if not isinstance(interface, OaNativeReleaseInterfaceReference):
        raise ValueError(
            f"Xcelium AMS dependency {ams.dependency} is not oa-native"
        )
    return interface.cell


def _resolved_circuit(
    spec: VerificationCellSpec,
) -> tuple[Path, Mapping[str, Any]]:
    ams = spec.ams
    assert ams is not None
    result = check_ip_integration(
        ams.integration_contract,
        project=spec.project,
        variant_name=ams.variant,
        fileset_name=ams.fileset,
    )
    matches = [
        dependency
        for dependency in result["dependency_releases"]
        if dependency.get("name") == ams.dependency
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Xcelium AMS fileset did not resolve dependency {ams.dependency!r}"
        )
    roles = matches[0].get("roles")
    if not isinstance(roles, Mapping) or ams.circuit_role not in roles:
        raise RuntimeError(
            "Xcelium AMS fileset did not resolve its selected circuit role"
        )
    relative = roles[ams.circuit_role]
    if not isinstance(relative, str):
        raise RuntimeError("Xcelium AMS circuit role path is invalid")
    artifact_root = spec.project.artifact_root.resolve()
    circuit = (artifact_root / Path(relative)).resolve()
    if not circuit.is_relative_to(artifact_root) or not circuit.is_file():
        raise RuntimeError("Xcelium AMS circuit role path is missing or unsafe")
    return circuit, result


def plan_xcelium_ams_cell(
    contract_path: Path,
    *,
    project: Project,
) -> XceliumAmsCellPlan:
    """Resolve one AMS cell without finding or launching an external simulator."""

    repository = project
    contract = resolve_xcelium_contract(contract_path, project=repository)
    spec = load_verification_cell(contract, project=repository)
    if spec.simulator.lower() != "xcelium-ams" or spec.ams is None:
        raise ValueError(
            f"verification cell {spec.cell} does not declare typed xcelium-ams inputs"
        )
    if spec.success_marker is None:
        raise ValueError(
            f"Xcelium AMS verification cell {spec.cell} must declare success_marker"
        )
    sources = (spec.canonical_source, *spec.compile_sources)
    if len(set(sources)) != len(sources):
        raise ValueError(f"verification cell {spec.cell} has duplicate compile sources")
    invalid_sources = [
        path for path in sources if path.suffix.lower() not in _AMS_HDL_SUFFIXES
    ]
    if invalid_sources:
        invalid = ", ".join(
            path.relative_to(repository.project_root).as_posix()
            for path in invalid_sources
        )
        raise ValueError(
            "Xcelium AMS compile inputs must be HDL/Verilog-AMS sources; "
            f"Spectre circuits must come from a locked release role: {invalid}"
        )
    circuit, integration_check = _resolved_circuit(spec)
    platform = load_platform(repository, spec.ams.platform)
    model_set = platform.simulation.model_set(spec.ams.model_set)
    model_names = [path.name for path in model_set.files]
    if len(model_names) != len(set(model_names)):
        raise ValueError("Xcelium AMS platform model files have duplicate basenames")
    native_cell = _native_dependency_cell(spec)
    command_template = (
        "xrun",
        "-64bit",
        "-timescale",
        "1ps/1ps",
        "-access",
        "+rwc",
        "-xmlibdirname",
        "$RUN_WORK/xcelium.d",
        "-log",
        "$RUN_WORK/xrun.log",
        *(path.relative_to(repository.project_root).as_posix() for path in sources),
        "$RUN_INPUTS/ams_control.scs",
    )
    source_paths = {
        contract,
        *spec.source_inputs,
        *spec.source_documents,
        *platform.source_paths,
        circuit,
        *model_set.files,
    }
    if spec.runner is not None:
        source_paths.add(spec.runner)
    return XceliumAmsCellPlan(
        contract=contract,
        spec=spec,
        sources=sources,
        circuit_netlist=circuit,
        circuit_sha256=_sha256(circuit),
        native_cell=native_cell,
        platform=platform,
        model_set=model_set,
        model_sha256={path: _sha256(path) for path in model_set.files},
        integration_check=integration_check,
        command_template=command_template,
        source_records=snapshot_verification_sources(
            source_paths,
            documents=(spec.source_documents, platform.source_documents),
        ),
    )


def execute_xcelium_ams_cell(
    plan: XceliumAmsCellPlan,
    *,
    artifacts: RunArtifacts,
    xrun: Path | None = None,
    before_spawn: Callable[[], None] | None = None,
    timeout: int = 600,
) -> XceliumExecution:
    """Execute a resolved AMS cell without creating or completing a run record."""

    staged: dict[str, Path] = {}

    def prepare_inputs() -> None:
        staged["circuit"] = artifacts.copy_file(
            "inputs",
            ("release", plan.circuit_netlist.name),
            plan.circuit_netlist,
        )
        staged.update(
            {
                f"model:{path.name}": artifacts.copy_file(
                    "inputs",
                    ("pdk", path.name),
                    path,
                )
                for path in plan.model_set.files
            }
        )
        staged["control"] = artifacts.write_text(
            "inputs",
            ("ams_control.scs",),
            plan.render_ams_control(
                circuit_netlist=staged["circuit"],
                model_file=staged[f"model:{plan.model_set.file.name}"],
            ),
        )

    return _execute_xcelium(
        plan,
        artifacts=artifacts,
        prepare_inputs=prepare_inputs,
        command_factory=lambda xrun_bin, work_path, xcelium_path: [
            str(xrun_bin),
            "-64bit",
            "-timescale",
            "1ps/1ps",
            "-access",
            "+rwc",
            "-xmlibdirname",
            xcelium_path,
            "-log",
            f"{work_path}/xrun.log",
            *(str(path) for path in plan.sources),
            str(staged["control"]),
        ],
        validate_inputs=lambda: _require_ams_inputs(plan),
        summary_fields={
            "evidence_role": "migration_regression",
            "product_qualification_conclusion": False,
        },
        before_spawn=before_spawn,
        xrun=xrun,
        timeout=timeout,
        run_process=run_process_group_capture,
        environment=xrun_env,
    )


def _require_ams_inputs(plan: XceliumAmsCellPlan) -> None:
    required = (
        *plan.spec.source_inputs,
        *plan.platform.source_paths,
        *plan.model_set.files,
        plan.circuit_netlist,
    )
    if any(not path.is_file() for path in required):
        raise FileNotFoundError("Xcelium AMS source input disappeared")
    if _sha256(plan.circuit_netlist) != plan.circuit_sha256:
        raise RuntimeError("Xcelium AMS locked circuit identity drift")
    if any(
        _sha256(path) != digest
        for path, digest in plan.model_sha256.items()
    ):
        raise RuntimeError("Xcelium AMS platform model identity drift")
