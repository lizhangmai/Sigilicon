"""Managed Xcelium AMS execution against one locked native-OA release."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
from collections.abc import Callable
import tomllib
from typing import Any, Mapping

from sigilicon.artifacts import read_nofollow_text
from sigilicon.domain.platform import PdkConfig, SimulationModelSet, load_platform
from sigilicon.project import Project
from sigilicon.domain.ip_release import RELEASE_MATURITY_LEVELS
from sigilicon.domain.verification_cell import VerificationCellSpec, load_verification_cell
from sigilicon.external_tools import (
    run_process_group_capture,
    xrun_env,
)
from sigilicon.workflows.run_artifacts import RunArtifacts
from sigilicon.workflows.ip_packaging import audit_ip_release_manifest
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
            "product_qualification_conclusion": False,
        }


def _project_source(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"Xcelium AMS {label} must be a project-relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"Xcelium AMS {label} must be a canonical relative path")
    path = (root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise ValueError(f"Xcelium AMS {label} is missing or unsafe")
    return path


def _toml(path: Path, label: str) -> dict[str, Any]:
    try:
        return tomllib.loads(read_nofollow_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Xcelium AMS {label} is invalid TOML") from exc


def _locked_native_release(
    spec: VerificationCellSpec,
) -> tuple[str, Path, Mapping[str, Any]]:
    """Resolve only the consumer-owned declaration, lock, and immutable package."""

    ams = spec.ams
    assert ams is not None
    component = _toml(ams.integration_contract, "integration contract")
    dependencies = component.get("component")
    matches = (
        [
            dependency
            for dependency in dependencies
            if isinstance(dependency, Mapping)
            and dependency.get("name") == ams.dependency
        ]
        if isinstance(dependencies, list)
        else []
    )
    if len(matches) != 1 or not isinstance(matches[0].get("release"), Mapping):
        raise ValueError(
            f"Xcelium AMS dependency is not one released dependency: {ams.dependency}"
        )
    release = matches[0]["release"]
    interface = release.get("interface")
    if (
        not isinstance(interface, Mapping)
        or interface.get("kind") != "oa-native"
        or any(
            not isinstance(interface.get(field), str) or not interface.get(field)
            for field in (
                "library",
                "cell",
                "schematic_view",
                "layout_view",
            )
        )
    ):
        raise ValueError(
            f"Xcelium AMS dependency {ams.dependency} is not oa-native"
        )
    variants = component.get("variants")
    if not isinstance(variants, Mapping) or ams.variant not in variants:
        raise ValueError("Xcelium AMS integration contract omits its variant")
    variant = _toml(
        _project_source(
            spec.project_root,
            variants[ams.variant],
            "variant contract",
        ),
        "variant contract",
    )
    try:
        required_roles = variant["filesets"][ams.fileset]["dependency_roles"][
            ams.dependency
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError("Xcelium AMS fileset omits its release roles") from exc
    if (
        not isinstance(required_roles, list)
        or ams.circuit_role not in required_roles
        or any(not isinstance(role, str) or not role for role in required_roles)
        or len(required_roles) != len(set(required_roles))
    ):
        raise ValueError("Xcelium AMS circuit role is not selected by its fileset")
    declared_roles = release.get("roles")
    if not isinstance(declared_roles, list) or not set(required_roles).issubset(
        declared_roles
    ):
        raise ValueError("Xcelium AMS fileset roles exceed its release declaration")

    lock = _toml(
        _project_source(
            spec.project_root,
            component.get("dependency_lock"),
            "dependency lock",
        ),
        "dependency lock",
    )
    pinned_dependencies = lock.get("dependency")
    pinned = (
        [
            item
            for item in pinned_dependencies
            if isinstance(item, Mapping) and item.get("name") == ams.dependency
        ]
        if isinstance(pinned_dependencies, list)
        else []
    )
    if len(pinned) != 1:
        raise ValueError("Xcelium AMS dependency lock is ambiguous")
    pin = pinned[0]
    artifact_root = spec.project.artifact_root.resolve()
    manifest_path = _project_source(
        artifact_root,
        pin.get("manifest"),
        "release manifest",
    )
    if _sha256(manifest_path) != pin.get("manifest_sha256"):
        raise ValueError("Xcelium AMS release manifest differs from its lock")
    manifest = audit_ip_release_manifest(manifest_path)
    if (
        manifest.get("ip_name") != ams.dependency
        or manifest.get("owner") != ams.dependency
        or manifest.get("release_id") != pin.get("release_id")
        or manifest.get("source_commit") != pin.get("source_commit")
    ):
        raise ValueError("Xcelium AMS release identity differs from its lock")
    export = release.get("export")
    exports = manifest.get("exports")
    selected_exports = (
        [item for item in exports if isinstance(item, Mapping) and item.get("name") == export]
        if isinstance(exports, list)
        else []
    )
    if len(selected_exports) != 1:
        raise ValueError("Xcelium AMS release export is missing or ambiguous")
    exported = selected_exports[0]
    exported_interface = exported.get("interface")
    if (
        not isinstance(exported_interface, Mapping)
        or exported_interface.get("kind") != interface["kind"]
    ):
        raise ValueError("Xcelium AMS release interface kind differs from intent")
    if exported.get("oa") != {
        field: interface[field]
        for field in ("library", "cell", "schematic_view", "layout_view")
    }:
        raise ValueError("Xcelium AMS release OA identity differs from intent")
    availability = exported.get("availability")
    if (
        not isinstance(availability, Mapping)
        or availability.get("simulation") is not True
    ):
        raise ValueError("Xcelium AMS release is unavailable for simulation")
    maturity = manifest.get("maturity")
    required_maturity = release.get("required_maturity")
    actual_maturity = (
        None if not isinstance(maturity, Mapping) else maturity.get("level")
    )
    if (
        actual_maturity not in RELEASE_MATURITY_LEVELS
        or required_maturity not in RELEASE_MATURITY_LEVELS
        or pin.get("maturity") != actual_maturity
        or RELEASE_MATURITY_LEVELS.index(actual_maturity)
        < RELEASE_MATURITY_LEVELS.index(required_maturity)
    ):
        raise ValueError("Xcelium AMS release maturity differs from its lock or intent")
    checks = maturity.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, Mapping) or check.get("passed") is not True
        for check in checks
    ):
        raise ValueError("Xcelium AMS release maturity checks are incomplete")
    provenance = manifest.get("provenance")
    dependency_contract = matches[0].get("contract")
    dependency_path = (
        PurePosixPath(dependency_contract)
        if isinstance(dependency_contract, str)
        else None
    )
    if (
        dependency_path is None
        or dependency_path.is_absolute()
        or "\\" in str(dependency_contract)
        or any(part in {"", ".", ".."} for part in dependency_path.parts)
        or len(dependency_path.parts) < 3
    ):
        raise ValueError("Xcelium AMS provider contract path is invalid")
    expected_producer = (
        PurePosixPath(*dependency_path.parts[:-2]).as_posix()
    )
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("working_tree_dirty") is not False
        or provenance.get("producer") != expected_producer
    ):
        raise ValueError("Xcelium AMS release provenance differs from its provider")
    artifacts = manifest.get("views")
    selected_roles = (
        [
            item
            for item in artifacts
            if isinstance(item, Mapping)
            and item.get("export") == export
            and item.get("role") in required_roles
        ]
        if isinstance(artifacts, list)
        else []
    )
    by_role = {item["role"]: item for item in selected_roles}
    if set(by_role) != set(required_roles) or len(selected_roles) != len(by_role):
        raise ValueError("Xcelium AMS package omits or duplicates a selected role")
    selected = by_role[ams.circuit_role]
    package_root = manifest_path.parent
    circuit = _project_source(
        package_root,
        selected.get("path"),
        "release circuit",
    )
    result = {
        "schema": 1,
        "contract_kind": "locked-release-selection",
        "passed": True,
        "dependency_releases": [
            {
                "name": ams.dependency,
                "export": export,
                "release_id": pin["release_id"],
                "source_commit": pin["source_commit"],
                "manifest": pin["manifest"],
                "manifest_sha256": pin["manifest_sha256"],
                "roles": {role: by_role[role]["path"] for role in required_roles},
            }
        ],
    }
    return interface["cell"], circuit, result


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
    native_cell, circuit, integration_check = _locked_native_release(spec)
    platform = load_platform(repository, spec.ams.platform)
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
    source_paths: Mapping[Path, Path] | None = None,
    xrun: Path | None = None,
    before_spawn: Callable[[], None] | None = None,
    environment_values: Mapping[str, str] | None = None,
    timeout: int = 600,
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
            *(str(selected(path)) for path in plan.sources),
            str(staged["control"]),
        ],
        validate_inputs=lambda: _require_ams_inputs(plan, source_paths=bound),
        summary_fields={
            "product_qualification_conclusion": False,
        },
        before_spawn=before_spawn,
        environment_values=environment_values,
        xrun=xrun,
        timeout=timeout,
        run_process=run_process_group_capture,
        environment=xrun_env,
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
