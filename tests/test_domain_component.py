from pathlib import Path

from sigilicon.domain.component import load_component_contract


def test_source_library_is_a_first_class_component_kind(tmp_path: Path) -> None:
    source = tmp_path / "ip/shared/library.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "ip/shared/ip.toml"
    contract.write_text(
        '''schema = 1
contract_kind = "ip-component"
path_scope = "owner"
owner = "shared"
name = "shared"
kind = "source-library"

[filesets]
python = ["ip/shared/library.py"]
''',
        encoding="utf-8",
    )

    loaded = load_component_contract(contract, project_root=tmp_path)

    assert loaded.kind == "source-library"
    assert loaded.filesets["python"][0].as_posix() == "ip/shared/library.py"
