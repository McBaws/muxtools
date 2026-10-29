from muxtools import SubFile, Setup, get_workdir
from muxtools.subtitle.subutils import create_document
from ass import Dialogue, Style
from pathlib import Path
from shutil import rmtree
from time import sleep
import pytest


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


def _write_sub(path: Path, styles: list[str], lines: list[Dialogue]) -> Path:
    doc = create_document()
    doc.styles.extend(Style(name=name) for name in styles)
    doc.events.extend(lines)
    with open(path, "w", encoding="utf_8_sig") as writer:
        doc.dump_file(writer)
    return path


def test_constructor_merge_dedupes_styles(tmp_path: Path) -> None:
    """
    A style that exists in several of the merged files (but not the first) is only added once.
    """
    files = [
        _write_sub(tmp_path / "a.ass", ["Default"], [Dialogue(style="Default", text="a")]),
        _write_sub(tmp_path / "b.ass", ["Default", "Sign"], [Dialogue(style="Sign", text="b")]),
        _write_sub(tmp_path / "c.ass", ["Default", "Sign"], [Dialogue(style="Sign", text="c")]),
    ]
    sub = SubFile(files)
    assert [style.name for style in sub._read_doc().styles] == ["Default", "Sign"]
