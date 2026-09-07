"""Prepare self-contained physical input for LC's library grouping pass."""
import re


def reference_lef(technology: str, cells: str) -> str:
    """Carry top-level VIA definitions into a cell-bearing LEF.

    compile_fusion_lib discards technology-only LEFs during grouping. It
    consequently loses via definitions needed by cell pins. Do not copy
    END LIBRARY, layer declarations or pin-local VIA references as definitions.
    """
    blocks = re.findall(r'^VIA[ \t]+([^\s;]+)[^\n]*\n.*?^END[ \t]+\1[ \t]*$',
                        technology, re.M | re.S)
    definitions = []
    for name in blocks:
        match = re.search(r'^VIA[ \t]+' + re.escape(name) + r'(?:[ \t][^\n]*)?\n.*?^END[ \t]+'
                          + re.escape(name) + r'[ \t]*$', technology, re.M | re.S)
        definitions.append(match.group(0))
    return ''.join(block + '\n' for block in definitions) + cells
