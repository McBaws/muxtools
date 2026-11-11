from muxtools import Setup, get_workdir, ensure_path, Chapters, get_executable, run_commandline
from shutil import rmtree
from time import sleep
from datetime import timedelta
from fractions import Fraction
from typing import cast
from muxtools.utils.types import LooseChapter
from muxtools.utils.log import LoggingException
import pytest

test_dir = ensure_path(__file__, None).parent


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


def test_add_chapters() -> None:
    ch = Chapters([], _print=False)
    existing = (timedelta(seconds=2), "Existing")
    ch.add(existing)
    ch.add((24, "First"))

    assert ch.chapters == [(timedelta(milliseconds=1001), "First"), existing]
    assert ch.chapters[1] is existing


@pytest.mark.parametrize("chapter", [(), (24,), (24, "name", "extra"), (timedelta(0),), (timedelta(0), "name", "extra")])
def test_add_rejects_malformed_chapter(chapter: tuple[object, ...]) -> None:
    ch = Chapters([(0, "Existing")], _print=False)
    before = ch.chapters.copy()

    with pytest.raises(ValueError):
        ch.add([(24, "Valid"), cast(LooseChapter, chapter)])

    assert ch.chapters == before


def test_chapters_from_mkv():
    f = test_dir / "test-data" / "sample-files" / "H265-Opus-EAC3-sample.mkv"
    ch = Chapters.from_mkv(f)

    assert ch.chapters


def test_chapters_from_sub():
    f = test_dir / "test-data" / "input" / "atri-subkt" / "ATRI 03 - Dialogue.ass"
    ch = Chapters.from_sub(f, use_actor_field=True)

    assert ch.chapters


def test_chapters_from_ogm():
    f = test_dir / "test-data" / "input" / "atri-subkt" / "ATRI 03 - Dialogue.ass"
    ch = Chapters.from_sub(f, use_actor_field=True)

    assert ch.chapters

    ogm = ch.to_file(minus_one_ms_hack=False)
    ch_ogm = Chapters(ogm)

    assert ch.chapters == ch_ogm.chapters


def test_unicode_chapters_with_legacy_charset():
    ch = Chapters([(0, "Prólogo"), (24, "日本語")], _print=False)
    ogm = ch.to_file(minus_one_ms_hack=False)
    assert Chapters(ogm, _print=False).chapters == ch.chapters

    source = test_dir / "test-data" / "sample-files" / "H264-MP3-Vorbis-sample.mkv"
    out = get_workdir() / "unicode-chapters.mkv"
    # A BOM must override the legacy charset even on systems that default to UTF-8.
    args = [get_executable("mkvmerge"), "-o", str(out), "--no-chapters", str(source), "--chapter-charset", "Windows-1252", "--chapters", str(ogm)]
    assert run_commandline(args, mkvmerge=True) <= 1

    extracted = get_workdir() / "extracted-chapters.txt"
    assert run_commandline([get_executable("mkvextract"), str(out), "chapters", "-s", str(extracted)]) == 0
    assert Chapters(extracted, _print=False).chapters == ch.chapters


def test_unnamed_chapters_are_written_without_a_name() -> None:
    ch = Chapters([(0, "Intro"), (240, "Part A"), (2400, "Part B")], _print=False).set_names(["Intro"])
    text = ch.to_file().read_text("utf-8-sig")

    assert "CHAPTER02NAME=\n" in text and "None" not in text


def test_set_names_keeps_caller_list() -> None:
    names: list[str | None] = ["A"]
    Chapters([(0, "x"), (24, "y")], _print=False).set_names(names)

    assert names == ["A"]


def test_shift_empty_chapters() -> None:
    assert Chapters([], _print=False).shift(5).chapters == []


def test_from_sub_markers_are_case_insensitive(tmp_path) -> None:
    ass = tmp_path / "chapters.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,chapter,{Prologue}\n"
        "Comment: 0,0:01:00.00,0:01:01.00,Default,,0,0,0,chapter,{A}\n",
        "utf-8",
    )

    for markers in ("Chapter", ["CHAPTER"], ["chapter"]):
        assert [name for _, name in Chapters.from_sub(ass, Fraction(24000, 1001), markers=markers, _print=False).chapters] == ["Prologue", "A"]


def test_from_mkv_without_chapters() -> None:
    with pytest.raises(LoggingException, match="does not contain any chapters"):
        Chapters.from_mkv(test_dir / "test-data" / "sample-files" / "H264-MP3-Vorbis-sample.mkv", _print=False)


def frames(ch: Chapters) -> list[tuple[int, str | None]]:
    return [(ch._frame(time), name) for time, name in ch.chapters]


@pytest.mark.parametrize(
    "trim, expected",
    [
        # The chapter playing at the cut point moves to the start
        ((50,), [(0, "Prologue"), (50, "OP"), (450, "A")]),
        ((100,), [(0, "OP"), (400, "A")]),
        ((150,), [(0, "OP"), (350, "A")]),
        # The end is a frame number of the untrimmed source
        ((100, 500), [(0, "OP")]),
        ((0, 501), [(0, "Prologue"), (100, "OP"), (500, "A")]),
        # The length of the trimmed clip bounds the end as well
        ((100, -50, 400), [(0, "OP")]),
        ((100, 0, 401), [(0, "OP"), (400, "A")]),
    ],
)
def test_trim(trim: tuple[int, ...], expected: list[tuple[int, str]]) -> None:
    ch = Chapters([(0, "Prologue"), (500, "A"), (100, "OP")], Fraction(24000, 1001), _print=False)

    assert frames(ch.trim(*trim)) == expected


def test_trim_negative_end_needs_num_frames() -> None:
    with pytest.raises(LoggingException):
        Chapters([(0, "A")], _print=False).trim(24, -24)


def test_trim_keeps_exact_times() -> None:
    times = [(timedelta(milliseconds=1234), "A"), (timedelta(milliseconds=5678), "B")]

    assert Chapters(list(times), _print=False).trim(0).chapters == times
