import codecs
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from ass import Dialogue, Style

from muxtools import SubFile, Setup
from muxtools.subtitle.subutils import create_document
from muxtools.utils.log import LoggingException

TEXT = "Café"


def _write_sub(path: Path, encoding: str, text: str = TEXT, bom: bytes = b"") -> Path:
    doc = create_document()
    doc.styles.append(Style(name="Default"))
    doc.events.append(Dialogue(start=timedelta(0), end=timedelta(seconds=1), style="Default", text=text))
    buffer = StringIO()
    doc.dump_file(buffer)
    path.write_bytes(bom + buffer.getvalue().encode(encoding))
    return path


def _assert_utf8_copy(sub: SubFile, text: str = TEXT):
    raw = sub.file.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert text.encode("utf_8") in raw
    assert sub._read_doc().events[0].text == text


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "work").mkdir()
    Setup("Test", None, work_dir=str(tmp_path / "work"))


@pytest.mark.parametrize(
    "encoding, bom",
    [
        ("utf_8_sig", b""),
        ("utf_8", b""),
        # the utf_16/utf_32 codecs write a little endian BOM, the _be ones none at all
        ("utf_16", b""),
        ("utf_16_be", codecs.BOM_UTF16_BE),
        ("utf_32", b""),
        ("utf_32_be", codecs.BOM_UTF32_BE),
    ],
)
def test_detects_unicode_encodings(tmp_path: Path, encoding: str, bom: bytes):
    _assert_utf8_copy(SubFile(_write_sub(tmp_path / "in.ass", encoding, bom=bom)))


def test_legacy_encoding_needs_to_be_passed(tmp_path: Path):
    path = _write_sub(tmp_path / "in.ass", "cp1252")
    with pytest.raises(LoggingException):
        SubFile(path)
    _assert_utf8_copy(SubFile(path, encoding="cp1252"))


def test_legacy_file_in_workdir_gets_converted(tmp_path: Path):
    path = _write_sub(tmp_path / "work" / "in.ass", "cp1252")
    original = path.read_bytes()
    sub = SubFile(path, encoding="cp1252")
    assert sub.file != path
    assert path.read_bytes() == original
    _assert_utf8_copy(sub)


def test_utf8_file_in_workdir_is_used_directly(tmp_path: Path):
    path = _write_sub(tmp_path / "work" / "in.ass", "utf_8_sig")
    assert SubFile(path).file == path


def test_merging_files_with_different_encodings(tmp_path: Path):
    first = _write_sub(tmp_path / "first.ass", "utf_16")
    second = _write_sub(tmp_path / "second.ass", "utf_8", "Naïve")
    sub = SubFile([first, second])
    assert [line.text for line in sub._read_doc().events] == [TEXT, "Naïve"]
    assert sub.file.read_bytes().startswith(b"\xef\xbb\xbf")


def test_merge_detects_encoding(tmp_path: Path):
    sub = SubFile(_write_sub(tmp_path / "main.ass", "utf_8_sig"))
    sub.merge(_write_sub(tmp_path / "other.ass", "utf_16", "Naïve"))
    assert [line.text for line in sub._read_doc().events] == [TEXT, "Naïve"]
