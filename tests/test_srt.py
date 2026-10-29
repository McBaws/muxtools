from muxtools import SubFile, Setup, get_workdir
from muxtools.subtitle.basesub import _Line
from muxtools.subtitle.srt import SRTEvent, _srt_tags_to_ass, parse_srt
from typing import cast
from shutil import rmtree
from time import sleep
import pytest


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


@pytest.mark.parametrize(
    "srt, ass",
    [
        (
            "<i>italic</i> <B>BOLD</B> <s>strike</s> <U>under</U> <i >spaced</i>",
            R"{\i1}italic{\i} {\b1}BOLD{\b} {\s1}strike{\s} {\u1}under{\u} {\i1}spaced{\i}",
        ),
        ("one<br>two<BR/>three<br />four", R"one\Ntwo\Nthree\Nfour"),
        ('<font color="#ff0000">red</font> <FONT COLOR=00ff00>green</FONT>', R"{\c&H0000FF&}red{\c} {\c&H00FF00&}green{\c}"),
        # Sizes are scaled from the 288 high script ffmpeg writes to 1080
        ("<font face='Comic Sans MS' size=\"24\">face</font> after", R"{\fnComic Sans MS\fs90}face{\fn\fs} after"),
        ('<font color="red" size="big">unsupported</font> </font>stray', "unsupported stray"),
        # Unknown tags are more likely to be text
        ("<Korean dialog> <<guillemets>> a < b > c", "<Korean dialog> <<guillemets>> a < b > c"),
    ],
)
def test_srt_tags(srt: str, ass: str) -> None:
    assert _srt_tags_to_ass(srt, font_tags=True) == ass


def test_srt_font_tags_removed() -> None:
    assert _srt_tags_to_ass('<font color="#ff0000" face="Arial" size="24">red</font> <i>text</i>') == R"red {\i1}text{\i}"


def test_parse_srt() -> None:
    srt = (
        "garbage\n\n"
        "1\n00:00:05,000 --> 00:00:06,000\n  later  \n\n"
        # '.' separator, a blank line inside the text and no blank line before the next event
        "2\n00:00:01.000 --> 00:00:02.000\nline one\n\nafter blank\n2019\n"
        # A cue that's just a number, and an exact duplicate of it
        "3\n00:00:03,000 --> 00:00:04,000\n42\n\n"
        "4\n00:00:03,000 --> 00:00:04,000\n42\n\n"
        # Ends before it starts so it lasts until the next one
        "5\n00:00:07,000 --> 00:00:06,000\n{i}negative{/i}\n\n"
        # No text, so it's dropped
        "6\n00:00:07,500 --> 00:00:07,600\n\n"
        # Odd spacing, single digit hours and junk after the timing
        "7\n 0:00:08,000 -->  0:00:09,000 line:0%\nlast\n2024\n"
    )
    assert parse_srt(srt.replace("\n", "\r\n")) == [
        SRTEvent(1000, 2000, R"line one\Nafter blank\N2019"),
        SRTEvent(3000, 4000, "42"),
        SRTEvent(5000, 6000, "later"),
        SRTEvent(7000, 8000, R"{\i1}negative{\i}"),
        SRTEvent(8000, 9000, R"last\N2024"),
    ]


@pytest.mark.parametrize("font_tags, text", [(False, R"{\i1}Hello{\i}\Nthere"), (True, R"{\i1}Hello{\i}\N{\c&H00FFFF&}there{\c}")])
def test_from_srt(tmp_path, font_tags: bool, text: str) -> None:
    srt = tmp_path / "ep.srt"
    # UTF-16 files are read as such even though the encoding defaults to UTF-8
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n<I>Hello</I>\n<font color=#ffff00>there</font>\n\n", encoding="utf-16")

    sub = SubFile.from_srt(srt, font_tags=font_tags)

    line = cast(_Line, sub._read_doc().events[0])
    assert line.text == text
