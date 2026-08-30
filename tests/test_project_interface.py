from __future__ import annotations

import subprocess
import sys

import sigilicon
from sigilicon.domain.repository import Project as DomainProject
from sigilicon.project import Project, ProjectFlow, ProjectOaWorkflow


def test_top_level_project_author_interface_is_narrow_and_canonical() -> None:
    assert sigilicon.__all__ == [
        "Project",
        "ProjectContext",
        "ProjectFlow",
        "ProjectOaWorkflow",
    ]
    assert Project is DomainProject
    assert sigilicon.ProjectFlow is ProjectFlow
    assert sigilicon.ProjectOaWorkflow is ProjectOaWorkflow


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


def test_builtin_registry_does_not_import_external_tool_adapters() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; "
            "from sigilicon.workflows.builtin import build_flow_registry; "
            "build_flow_registry(); "
            "forbidden = {"
            "'sigilicon.workflows.synopsys', "
            "'sigilicon.workflows.calibre_pex', "
            "'sigilicon.workflows.layout_verification', "
            "'sigilicon.workflows.oa_materialization', "
            "'sigilicon.workflows.physical_design'}; "
            "assert forbidden.isdisjoint(sys.modules), "
            "sorted(forbidden & set(sys.modules))",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
