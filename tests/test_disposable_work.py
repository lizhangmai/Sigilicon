from pathlib import Path

from sigilicon.virtuoso.disposable import DisposableWork


def test_disposable_work_has_no_manifest_and_cleans_exact_root(tmp_path: Path) -> None:
    root = tmp_path / "oa-work"
    with DisposableWork(root) as work:
        staged = work.write_text("inputs", ("source.scs",), "canonical\n")
        assert staged.read_text(encoding="utf-8") == "canonical\n"
        assert not hasattr(work, "manifest")

    assert not root.exists()


def test_disposable_work_keep_detaches_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "oa-result"
    work = DisposableWork(root)

    retained = work.keep()

    assert retained == root.resolve()
    assert retained.is_dir()
    work.cleanup()
    assert retained.is_dir()
