from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from sigilicon.flow import (
    ActionContext,
    FlowExecutionError,
    InputArtifact,
    ResolvedCapability,
)
from sigilicon.flow.standard_asic import register_standard_asic_actions
from sigilicon.flow.registry import FlowRegistry
from sigilicon.paths import ProjectContext, ProjectScope
import sigilicon.workflows.synopsys.structural_link as synopsys


def _manifest(
    path: Path,
    *,
    kind: str,
    qualifiers: dict[str, str],
    members: list[tuple[str, Path]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "contract_kind": "source-set-manifest",
                "kind": kind,
                "qualifiers": qualifiers,
                "members": [
                    {
                        "path": name,
                        "file": member.relative_to(path.parent).as_posix(),
                    }
                    for name, member in members
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _tool(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_structural_link_adapter_owns_both_synopsys_stages_and_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "project"
    owner_root = project_root / "ip/fixture"
    artifact_root = project_root / "artifacts"
    owner_root.mkdir(parents=True)
    artifact_root.mkdir()
    source = owner_root / "rtl/top.sv"
    source.parent.mkdir()
    source.write_text("module top; endmodule\n", encoding="utf-8")

    input_root = tmp_path / "inputs"
    rtl_snapshot = input_root / "rtl/files/rtl/top.sv"
    rtl_snapshot.parent.mkdir(parents=True)
    rtl_snapshot.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    rtl_manifest = input_root / "rtl/manifest.json"
    _manifest(
        rtl_manifest,
        kind="source-set.systemverilog",
        qualifiers={"variant": "fixture"},
        members=[("rtl/top.sv", rtl_snapshot)],
    )

    recipe_root = input_root / "recipe"
    recipe = recipe_root / "files/implementation/structural.toml"
    compile_script = recipe_root / "files/implementation/compile.tcl"
    link_script = recipe_root / "files/implementation/link.tcl"
    variant = recipe_root / "files/configs/variant.toml"
    component = recipe_root / "files/configs/ip.toml"
    dependency_lock = recipe_root / "files/configs/dependency.lock.toml"
    filelist = recipe_root / "files/rtl/synth.f"
    for path in (
        recipe,
        compile_script,
        link_script,
        variant,
        component,
        dependency_lock,
        filelist,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    recipe.write_text(
        '''schema = 1
contract_kind = "ip-structural-link-recipe"
path_scope = "owner"
owner = "fixture"
component_contract = "configs/ip.toml"
dependency_lock = "configs/dependency.lock.toml"
provider_owner = "ip/macro"
fileset = "synthesis"
liberty_role = "raw_macro_liberty_or_db"
liberty_compile_script = "implementation/compile.tcl"
link_script = "implementation/link.tcl"
library_name = "fixture_structural"
macro_cell = "FIXTURE_MACRO"
[parameter_overrides]
BLOCKS = 1
[variants]
fixture = "configs/variant.toml"
[expected]
macro_instances = 1
unresolved_references = 0
[claims]
timing_characterized = false
power_characterized = false
area_characterized = false
''',
        encoding="utf-8",
    )
    compile_script.write_text("# fixture\n", encoding="utf-8")
    link_script.write_text("# fixture\n", encoding="utf-8")
    variant.write_text(
        '''schema = 1
contract_kind = "ip-operating-variant"
path_scope = "variant"
owner = "fixture"
name = "fixture-variant"

[integration]
component_contract = "ip/fixture/configs/ip.toml"
variant = "fixture"

[filesets.synthesis]
filelist = "ip/fixture/rtl/synth.f"
top_module = "fixture_top"
required_capability = "synthesis"

[filesets.synthesis.dependency_roles]
macro = ["raw_macro_liberty_or_db"]
''',
        encoding="utf-8",
    )
    component.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "fixture"
name = "fixture"
dependency_lock = "ip/fixture/configs/dependency.lock.toml"

[variants]
fixture = "ip/fixture/configs/variant.toml"

[[component]]
name = "macro"
contract = "ip/macro/configs/ip.toml"

[component.release]
export = "macro-export"
required_maturity = "development"
roles = ["raw_macro_liberty_or_db"]

[component.release.interface]
kind = "oa-native"
library = "macro"
cell = "FIXTURE_MACRO"
schematic_view = "schematic"
layout_view = "layout"
''',
        encoding="utf-8",
    )
    filelist.write_text("ip/fixture/rtl/top.sv\n", encoding="utf-8")

    macro_liberty = artifact_root / "release/macro.lib"
    macro_liberty.parent.mkdir()
    macro_liberty.write_text("library (fixture) {}\n", encoding="utf-8")
    release_manifest = artifact_root / "release/manifest.json"
    release_manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "contract_kind": "ip-release-manifest",
                "release_kind": "source-package",
                "owner": "macro",
                "ip_name": "macro",
                "release_id": "fixture-release",
                "source_commit": "a" * 40,
                "component": {
                    "name": "macro",
                    "kind": "composite-ip",
                    "contract": "ip/macro/configs/ip.toml",
                },
                "provenance": {
                    "producer": "ip/macro",
                    "contract": "ip/macro/configs/release.toml",
                    "working_tree_dirty": False,
                },
                "source_files": [
                    "ip/macro/configs/interface.toml",
                    "ip/macro/configs/ip.toml",
                    "ip/macro/configs/release.toml",
                    "ip/fixture/release/macro.lib",
                ],
                "maturity": {
                    "level": "development",
                    "checks": [{"passed": True}],
                },
                "exports": [
                    {
                        "name": "macro-export",
                        "availability": {"synthesis": True},
                        "interface": {
                            "kind": "oa-native",
                            "contract": "ip/macro/configs/interface.toml",
                        },
                        "oa": {
                            "library": "macro",
                            "cell": "FIXTURE_MACRO",
                            "schematic_view": "schematic",
                            "layout_view": "layout",
                        },
                        "maturity": {
                            "required_roles": [],
                        },
                    }
                ],
                "views": [
                    {
                        "export": "macro-export",
                        "role": "raw_macro_liberty_or_db",
                        "path": "macro.lib",
                        "source": "ip/fixture/release/macro.lib",
                        "size": macro_liberty.stat().st_size,
                        "capabilities": ["synthesis"],
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    def write_dependency_lock(
        manifest_path: str = "release/manifest.json",
    ) -> None:
        dependency_lock.write_text(
            f'''schema = 1
contract_kind = "ip-dependency-lock"
path_scope = "owner"
owner = "fixture"
ip = "fixture"

[[dependency]]
name = "macro"
release_id = "fixture-release"
manifest = "{manifest_path}"
maturity = "development"
source_commit = "{'a' * 40}"
manifest_sha256 = "{sha256(release_manifest.read_bytes()).hexdigest()}"
''',
            encoding="utf-8",
        )

    write_dependency_lock()
    live_component = owner_root / "configs/ip.toml"
    live_lock = owner_root / "configs/dependency.lock.toml"
    live_component.parent.mkdir()
    live_component.write_bytes(component.read_bytes())
    live_lock.write_bytes(dependency_lock.read_bytes())
    recipe_manifest = recipe_root / "manifest.json"
    recipe_members = [
        ("implementation/structural.toml", recipe),
        ("implementation/compile.tcl", compile_script),
        ("implementation/link.tcl", link_script),
        ("configs/variant.toml", variant),
        ("configs/ip.toml", component),
        ("configs/dependency.lock.toml", dependency_lock),
        ("rtl/synth.f", filelist),
    ]
    _manifest(
        recipe_manifest,
        kind="recipe.structural-link",
        qualifiers={"variant": "fixture"},
        members=recipe_members,
    )
    monkeypatch.setattr(
        synopsys,
        "run_readonly_capture",
        lambda *_args, **_kwargs: macro_liberty.read_bytes(),
    )
    audit_calls: list[Path] = []

    def audit_fixture(path: Path) -> dict[str, object]:
        audit_calls.append(path)
        return json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(synopsys, "audit_ip_release_manifest", audit_fixture)
    monkeypatch.setenv("FIXTURE_LIVE_LOCK", str(live_lock))

    library_compiler = _tool(
        tmp_path / "lc_shell",
        '''import os
from pathlib import Path
Path(os.environ["SIGILICON_STRUCTURAL_DB"]).write_text("db\\n")
print("SIGILICON_STRUCTURAL_DB_PASS library=" + os.environ["SIGILICON_STRUCTURAL_LIBRARY"])
''',
    )
    design_compiler = _tool(
        tmp_path / "dc_shell",
        '''import os
from pathlib import Path
sources = Path(os.environ["SIGILICON_STRUCTURAL_SOURCES"])
assert len(sources.read_text().splitlines()) == 1
Path(os.environ["SIGILICON_STRUCTURAL_REPORT"]).write_text("passed\\n")
Path(os.environ["SIGILICON_STRUCTURAL_CHECKPOINT"]).write_text("ddc\\n")
Path(os.environ["FIXTURE_LIVE_LOCK"]).write_text("mutated after execution\\n")
print("SIGILICON_STRUCTURAL_LINK_PASS top=fixture_top macro_instances=1 unresolved=0")
''',
    )

    registry = FlowRegistry()
    register_standard_asic_actions(registry)
    action = registry.action("asic.structural-link")
    project = ProjectContext.from_roots(
        project_root,
        artifact_root=artifact_root,
        workspace_root=project_root / "workspace",
    )
    scope = ProjectScope._from_cataloged_owner(project, "fixture", owner_root)
    run_root = artifact_root / "runs/fixture"
    work_root = run_root / "work"
    output_root = run_root / "outputs"
    log_root = run_root / "logs"
    for path in (work_root, output_root, log_root):
        path.mkdir(parents=True)
    context = ActionContext(
        node_id="link",
        action=action,
        run_root=run_root,
        work_root=work_root,
        output_root=output_root,
        log_root=log_root,
        inputs={
            "rtl-sources": InputArtifact(
                "rtl-sources",
                "source-set.systemverilog",
                rtl_manifest,
                "assets",
                qualifiers={"variant": "fixture"},
            ),
            "structural-link-recipe": InputArtifact(
                "structural-link-recipe",
                "recipe.structural-link",
                recipe_manifest,
                "assets",
                qualifiers={"variant": "fixture"},
            ),
        },
        action_config={
            "recipe": "implementation/structural.toml",
            "variant": "fixture",
        },
        adapter_config={"timeout_seconds": 30},
        capabilities={
            "tool.synopsys-library-compiler": ResolvedCapability(
                "fixture-lc",
                executable=library_compiler,
            ),
            "tool.synopsys-dc": ResolvedCapability(
                "fixture-dc",
                executable=design_compiler,
            ),
        },
        platform_assets={},
        project_scope=scope,
    )

    adapter = synopsys.SynopsysStructuralLinkAdapter()
    recipe_snapshot, recipe_snapshot_members = adapter._recipe(context)
    original_manifest = json.loads(release_manifest.read_text(encoding="utf-8"))
    release_alias = artifact_root / "release-alias"
    release_alias.symlink_to(release_manifest.parent, target_is_directory=True)
    write_dependency_lock("release-alias/manifest.json")
    with pytest.raises(FlowExecutionError, match="contains a symlink"):
        adapter._integration(
            context,
            recipe_snapshot,
            recipe_snapshot_members,
        )
    release_alias.unlink()
    write_dependency_lock()

    invalid_releases = (
        (
            lambda raw: raw["provenance"].update(working_tree_dirty=True),
            "provider provenance",
        ),
        (
            lambda raw: raw["provenance"].update(producer="ip"),
            "provider provenance",
        ),
        (
            lambda raw: raw["component"].update(
                contract="ip/other/configs/ip.toml"
            ),
            "provider provenance",
        ),
        (
            lambda raw: raw["exports"][0]["oa"].update(cell="OTHER_MACRO"),
            "integration intent",
        ),
    )
    for mutate, message in invalid_releases:
        invalid = json.loads(json.dumps(original_manifest))
        mutate(invalid)
        release_manifest.write_text(
            json.dumps(invalid) + "\n",
            encoding="utf-8",
        )
        write_dependency_lock()
        with pytest.raises(FlowExecutionError, match=message):
            adapter._integration(
                context,
                recipe_snapshot,
                recipe_snapshot_members,
            )

    release_manifest.write_text(
        json.dumps(original_manifest) + "\n",
        encoding="utf-8",
    )
    write_dependency_lock()
    audit_calls.clear()

    result = adapter.run(context)

    assert result.execution.status == "succeeded"
    assert audit_calls == [release_manifest, release_manifest, release_manifest]
    assert live_lock.read_text(encoding="utf-8") == "mutated after execution\n"
    assert result.collected is not None
    assert result.collected.facts.as_mapping() == {
        "evidence-role": "regression",
        "evidence-level": "l4",
        "evidence-scope": "native-macro-structural-link",
        "product-qualification-conclusion": False,
        "macro-instance-count": 1,
        "unresolved-reference-count": 0,
        "timing-characterized": False,
        "power-characterized": False,
        "area-characterized": False,
    }
    assert {artifact.role for artifact in result.collected.artifacts} == {
        "compiled-macro-library",
        "checkpoint",
        "structural-report",
        "evidence",
    }
    evidence = json.loads(
        next(
            artifact.path.read_text(encoding="utf-8")
            for artifact in result.collected.artifacts
            if artifact.role == "evidence"
        )
    )
    assert evidence["release_id"] == "fixture-release"
    assert evidence["macro_instances"] == 1
    assert evidence["macro_liberty_sha256"] == evidence["pinned_source_sha256"]
