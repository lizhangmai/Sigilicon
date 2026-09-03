"""Managed Xcelium AMS execution against one locked native-OA release."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from collections.abc import Callable
from types import MappingProxyType
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.platform import (
    PdkConfig,
    SimulationModelSet,
    load_platform,
    model_resource_identities,
)
from sigilicon.project import Project
from sigilicon.release_store import ReleaseRef, ReleaseStore
from sigilicon.domain.verification_cell import (
    VerificationCellSpec,
    XceliumAmsReleaseCircuit,
    XceliumAmsSourceCircuit,
    load_verification_cell,
)
from sigilicon.external_tools import ProcessPort, managed_process
from sigilicon.execution._model import Resources
from sigilicon.execution._workspace import StepWorkspace
from sigilicon.workflows.ip_packaging import validate_ip_release_package
from sigilicon.workflows.ip_integration import check_ip_integration
from sigilicon.workflows.xcelium import (
    XceliumCellPlan,
    XceliumExecution,
    execute_xcelium_invocation,
    resolve_xcelium_contract,
    snapshot_verification_sources,
)


_AMS_HDL_SUFFIXES = frozenset({".sv", ".v", ".vams", ".va"})


def _sha256(path: Path) -> str:
    return hashlib.sha256(read_nofollow_text(path).encode("utf-8")).hexdigest()


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
    resource_identities: Mapping[Path, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_records",
            MappingProxyType(dict(self.source_records)),
        )
        object.__setattr__(
            self,
            "model_sha256",
            MappingProxyType(dict(self.model_sha256)),
        )
        object.__setattr__(
            self,
            "integration_check",
            MappingProxyType(dict(self.integration_check)),
        )
        object.__setattr__(
            self,
            "resource_identities",
            MappingProxyType(dict(self.resource_identities)),
        )

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
        model_resources = [
            {
                "identity": (
                    f"pdk:{self.platform.key}:simulation/"
                    f"{self.model_set.name}/{index}-{path.name}"
                ),
                "sha256": self.model_sha256[path],
            }
            for index, path in enumerate(self.model_set.files)
        ]
        return {
            **self.spec.as_dict(),
            "contract": self.contract.relative_to(root).as_posix(),
            "sources": [
                path.relative_to(root).as_posix() for path in self.sources
            ],
            "circuit": {
                "cell": self.native_cell,
                "circuit_sha256": self.circuit_sha256,
                "selection": dict(self.integration_check),
            },
            "platform_model": {
                "platform": self.platform.key,
                "model_set": self.model_set.name,
                "section": self.model_set.single_section,
                "resources": model_resources,
            },
            "command_template": list(self.command_template),
            "product_qualification_conclusion": False,
        }


def _locked_native_release(
    spec: VerificationCellSpec,
    circuit_selection: XceliumAmsReleaseCircuit,
) -> tuple[str, Path, Mapping[str, Any], Mapping[Path, str]]:
    """Resolve one native circuit through the canonical integration workflow."""

    try:
        selection = check_ip_integration(
            circuit_selection.contract,
            project=spec.project,
            variant_name=circuit_selection.variant,
            fileset_name=circuit_selection.fileset,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        raise ValueError(
            f"Xcelium AMS integration selection is invalid: {exc}"
        ) from exc
    matches = [
        dependency
        for dependency in selection["dependency_releases"]
        if dependency["name"] == circuit_selection.dependency
    ]
    if len(matches) != 1:
        raise ValueError("Xcelium AMS dependency release is missing or ambiguous")
    selected = matches[0]
    roles = selected["roles"]
    if circuit_selection.role not in roles:
        raise ValueError("Xcelium AMS circuit role is not selected by its fileset")
    ref = ReleaseRef(selected["store"], selected["manifest_sha256"])
    try:
        audited = ReleaseStore.from_artifact_root(spec.project.artifact_root).open(
            ref,
            validate=validate_ip_release_package,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise ValueError("Xcelium AMS release manifest is invalid") from exc
    manifest_path = audited.manifest_path
    manifest = audited.manifest
    manifest_digest = ref.manifest_sha256
    export = selected["export"]
    exports = manifest.get("exports")
    selected_exports = (
        [
            item
            for item in exports
            if isinstance(item, Mapping) and item.get("name") == export
        ]
        if isinstance(exports, list)
        else []
    )
    if len(selected_exports) != 1:
        raise ValueError("Xcelium AMS release export is missing or ambiguous")
    exported = selected_exports[0]
    exported_interface = exported.get("interface")
    oa = exported.get("oa")
    if (
        not isinstance(exported_interface, Mapping)
        or exported_interface.get("kind") != "oa-native"
        or not isinstance(oa, Mapping)
        or any(
            not isinstance(oa.get(field), str) or not oa.get(field)
            for field in ("library", "cell", "schematic_view", "layout_view")
        )
    ):
        raise ValueError("Xcelium AMS release export is not a native OA interface")
    availability = exported.get("availability")
    if (
        not isinstance(availability, Mapping)
        or availability.get("simulation") is not True
    ):
        raise ValueError("Xcelium AMS release is unavailable for simulation")
    try:
        circuit_artifact = audited.role(export, circuit_selection.role)
    except RuntimeError as exc:
        raise ValueError(
            "Xcelium AMS release circuit is missing or unsafe"
        ) from exc
    circuit = circuit_artifact.path
    circuit_digest = circuit_artifact.sha256
    result = {
        "schema": 1,
        "contract_kind": "locked-release-selection",
        "passed": True,
        "dependency_releases": [selected],
    }
    release_sources = MappingProxyType(
        {
            manifest_path: manifest_digest,
            circuit: circuit_digest,
        }
    )
    return str(oa["cell"]), circuit, result, release_sources


def _resolve_circuit(
    spec: VerificationCellSpec,
) -> tuple[str, Path, Mapping[str, Any], Mapping[Path, str]]:
    ams = spec.ams
    assert ams is not None
    circuit = ams.circuit
    if isinstance(circuit, XceliumAmsReleaseCircuit):
        return _locked_native_release(spec, circuit)
    assert isinstance(circuit, XceliumAmsSourceCircuit)
    digest = _sha256(circuit.path)
    return (
        circuit.cell,
        circuit.path,
        MappingProxyType(
            {
                "schema": 1,
                "contract_kind": "source-circuit-selection",
                "passed": True,
                "source": circuit.path.relative_to(spec.project_root).as_posix(),
                "cell": circuit.cell,
            }
        ),
        MappingProxyType({circuit.path: digest}),
    )


def _external_resource_identities(
    spec: VerificationCellSpec,
    platform: PdkConfig,
    model_set: SimulationModelSet,
    circuit: Path,
    selection: Mapping[str, Any],
    circuit_records: Mapping[Path, str],
) -> Mapping[Path, str]:
    """Bind PDK and immutable-release inputs at their owning domain seam."""

    selected = dict(model_resource_identities(platform, model_set))
    ams = spec.ams
    assert ams is not None
    if isinstance(ams.circuit, XceliumAmsSourceCircuit):
        return MappingProxyType(selected)
    releases = selection.get("dependency_releases")
    if not isinstance(releases, list) or len(releases) != 1:
        raise ValueError("Xcelium AMS release identity is unavailable")
    release = releases[0]
    if not isinstance(release, Mapping):
        raise ValueError("Xcelium AMS release identity is invalid")
    dependency = release.get("name")
    release_id = release.get("release_id")
    if not isinstance(dependency, str) or not isinstance(release_id, str):
        raise ValueError("Xcelium AMS release identity is incomplete")
    prefix = f"release:{dependency}:{release_id}"
    selected[circuit.absolute()] = f"{prefix}/role/{ams.circuit.role}"
    manifests = tuple(
        path
        for path in circuit_records
        if path != circuit and path.name == "manifest.json"
    )
    if len(manifests) > 1:
        raise ValueError("Xcelium AMS release manifest identity is ambiguous")
    if manifests:
        selected[manifests[0].absolute()] = f"{prefix}/manifest"
    return MappingProxyType(selected)


def plan_xcelium_ams_cell(
    contract_path: Path,
    *,
    project: Project,
    resources: Resources,
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
            f"Spectre circuits must come from the AMS circuit selection: {invalid}"
        )
    native_cell, circuit, integration_check, release_records = (
        _resolve_circuit(spec)
    )
    platform = load_platform(
        repository,
        spec.ams.platform,
        resources=resources,
    )
    model_set = platform.simulation.model_set(spec.ams.model_set)
    model_names = [path.name for path in model_set.files]
    if len(model_names) != len(set(model_names)):
        raise ValueError("Xcelium AMS platform model files have duplicate basenames")
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
        *release_records,
        *model_set.files,
    }
    if spec.runner is not None:
        source_paths.add(spec.runner)
    model_sha256 = {path: _sha256(path) for path in model_set.files}
    source_records = snapshot_verification_sources(
        source_paths,
        documents=(spec.source_documents, platform.source_documents),
    )
    expected_snapshots = {**release_records, **model_sha256}
    if any(
        hashlib.sha256(source_records[path].encode("utf-8")).hexdigest()
        != digest
        for path, digest in expected_snapshots.items()
    ):
        raise ValueError("Xcelium AMS input changed while its plan was being bound")
    return XceliumAmsCellPlan(
        contract=contract,
        spec=spec,
        sources=sources,
        circuit_netlist=circuit,
        circuit_sha256=release_records[circuit],
        native_cell=native_cell,
        platform=platform,
        model_set=model_set,
        model_sha256=model_sha256,
        integration_check=integration_check,
        resource_identities=_external_resource_identities(
            spec,
            platform,
            model_set,
            circuit,
            integration_check,
            release_records,
        ),
        command_template=command_template,
        source_records=source_records,
    )


def execute_xcelium_ams_cell(
    plan: XceliumAmsCellPlan,
    *,
    artifacts: StepWorkspace,
    source_paths: Mapping[Path, Path] | None = None,
    resources: Resources,
    before_spawn: Callable[[], None] | None = None,
    environment_values: Mapping[str, str] | None = None,
    timeout: int = 600,
    process: ProcessPort = managed_process,
) -> XceliumExecution:
    """Execute a resolved AMS cell without creating or completing a run record."""

    staged: dict[str, Path] = {}
    bound = {} if source_paths is None else {
        Path(source).resolve(): Path(value).resolve()
        for source, value in source_paths.items()
    }

    def selected(path: Path) -> Path:
        return bound.get(path.resolve(), path)

    def prepare_inputs() -> None:
        staged["circuit"] = artifacts.copy_file(
            "inputs",
            ("release", plan.circuit_netlist.name),
            selected(plan.circuit_netlist),
        )
        staged.update(
            {
                f"model:{path.name}": artifacts.copy_file(
                    "inputs",
                    ("pdk", path.name),
                    selected(path),
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

    return execute_xcelium_invocation(
        artifacts=artifacts,
        plan_record=plan.as_dict(),
        cell=plan.spec.cell,
        dut=plan.spec.dut,
        success_marker=plan.spec.success_marker,
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
            *(str(selected(path)) for path in plan.sources),
            str(staged["control"]),
        ],
        validate_inputs=lambda: _require_ams_inputs(plan, source_paths=bound),
        summary_fields={
            "product_qualification_conclusion": False,
        },
        before_spawn=before_spawn,
        environment_values=environment_values,
        resources=resources,
        timeout=timeout,
        process=process,
    )


def _require_ams_inputs(
    plan: XceliumAmsCellPlan,
    *,
    source_paths: Mapping[Path, Path] | None = None,
) -> None:
    bound = {} if source_paths is None else source_paths

    def selected(path: Path) -> Path:
        return bound.get(path.resolve(), path)

    required = (
        *plan.spec.source_inputs,
        *plan.platform.source_paths,
        *plan.model_set.files,
        plan.circuit_netlist,
    )
    if any(not selected(path).is_file() for path in required):
        raise FileNotFoundError("Xcelium AMS source input disappeared")
    if _sha256(selected(plan.circuit_netlist)) != plan.circuit_sha256:
        raise RuntimeError("Xcelium AMS locked circuit identity drift")
    if any(
        _sha256(selected(path)) != digest
        for path, digest in plan.model_sha256.items()
    ):
        raise RuntimeError("Xcelium AMS platform model identity drift")
