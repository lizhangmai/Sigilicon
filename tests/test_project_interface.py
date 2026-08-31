from __future__ import annotations

import subprocess
import sys

import pytest

import sigilicon
from sigilicon.cli.agentic_execute import _parser as execute_parser
from sigilicon.cli.agentic_read import _parser as read_parser
from sigilicon.domain.repository import Project as DomainProject
from sigilicon.project import (
    Project,
    ProjectExecution,
    ProjectOaWorkflow,
    ProjectRunner,
)


def test_top_level_project_author_interface_is_narrow_and_canonical() -> None:
    assert sigilicon.__all__ == []
    assert Project is DomainProject
    assert not hasattr(sigilicon, "Project")
    assert not hasattr(sigilicon, "ProjectExecution")
    assert not hasattr(sigilicon, "ProjectRunner")
    assert not hasattr(sigilicon, "ProjectOaWorkflow")


def test_plain_package_import_does_not_load_virtuoso_modules() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys, sigilicon; "
            "assert not any(name.startswith('sigilicon.virtuoso') "
            "for name in sys.modules)",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_generic_flow_interface_does_not_aggregate_domain_action_modules() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; import sigilicon.flow as flow; "
            "assert {'ActionBinding', 'ExecutionRecipe', "
            "'compile_flow_spec', 'parse_execution_recipe'} <= set(flow.__all__); "
            "assert {'AdapterSelection', 'ExecutionProfile'}"
            ".isdisjoint(flow.__all__); "
            "assert 'register_standard_asic_actions' not in flow.__all__; "
            "forbidden = {"
            "'sigilicon.flow.circuit_design', "
            "'sigilicon.flow.physical_design', "
            "'sigilicon.flow.physical_verification', "
            "'sigilicon.flow.post_layout', "
            "'sigilicon.flow.standard_asic'}; "
            "assert forbidden.isdisjoint(sys.modules), "
            "sorted(forbidden & set(sys.modules))",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_stable_flow_and_agentic_imports_do_not_load_experimental_modules() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; "
            "import sigilicon.flow; "
            "import sigilicon.workflows.agentic_read; "
            "import sigilicon.workflows.agentic_execution; "
            "import sigilicon.cli.flow_core; "
            "forbidden = {name for name in sys.modules if name == 'sigilicon.experimental' "
            "or name.startswith('sigilicon.experimental.')}; "
            "assert not forbidden, sorted(forbidden)",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_stable_agentic_cli_rejects_experimental_campaign_routes() -> None:
    with pytest.raises(SystemExit):
        read_parser().parse_args(
            ["--project-root", "/tmp/project", "campaign-plan"]
        )
    with pytest.raises(SystemExit):
        execute_parser().parse_args(
            [
                "--project-root",
                "/tmp/project",
                "--grant",
                "/tmp/grant.json",
                "campaign-run",
            ]
        )


def test_action_registry_does_not_import_external_tool_adapters() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; "
            "from sigilicon.workflows.action_registry import build_action_registry; "
            "build_action_registry(); "
            "forbidden = {"
            "'sigilicon.workflows.synopsys', "
            "'sigilicon.workflows.calibre_pex', "
            "'sigilicon.workflows.layout_verification', "
            "'sigilicon.workflows.physical_design', "
            "'sigilicon.experimental.workflows.oa_materialization', "
            "'sigilicon.experimental.workflows.layout_flow', "
            "'sigilicon.experimental.workflows.reference_physical_design'}; "
            "assert forbidden.isdisjoint(sys.modules), "
            "sorted(forbidden & set(sys.modules))",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_action_registry_registers_reference_pnr_without_a_second_builder() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "from sigilicon.workflows.action_registry import build_action_registry; "
            "registry = build_action_registry(); "
            "assert registry.has_adapter('reference-pnr'); "
            "assert registry.action('physical-design.reference-solve').kind "
            "== 'physical-design.reference-solve'",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_registry_materializes_only_the_selected_synopsys_tool_module() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; "
            "from sigilicon.workflows.action_registry import build_action_registry; "
            "registry = build_action_registry(); "
            "registry.adapter('synopsys-dc'); "
            "assert 'sigilicon.workflows.synopsys.dc' in sys.modules; "
            "forbidden = {"
            "'sigilicon.workflows.synopsys.fc', "
            "'sigilicon.workflows.synopsys.hspice', "
            "'sigilicon.workflows.synopsys.structural_link', "
            "'sigilicon.workflows.synopsys.vcs'}; "
            "assert forbidden.isdisjoint(sys.modules), "
            "sorted(forbidden & set(sys.modules))",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_synopsys_package_does_not_reexport_tool_adapters() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sigilicon.workflows.synopsys as synopsys; "
            "assert not hasattr(synopsys, 'SynopsysDCAdapter'); "
            "assert not hasattr(synopsys, 'SynopsysVCSAdapter')",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
