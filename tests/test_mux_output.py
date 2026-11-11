import json
import re
import subprocess
from pathlib import Path

import pytest

from muxtools import Setup, Premux, mux, get_executable, TmdbConfig
from muxtools.muxing.muxer import output_names
from muxtools.muxing.tmdb import EpisodeMetadata, MediaMetadata
from muxtools.muxing.token_handling import TOKEN_REGEX, apply_dynamic_tokens
from muxtools.utils.log import LoggingException

sample = Path(__file__).parent / "test-data" / "sample-files" / "H264-10bit-FLAC-sample.mkv"


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup("01", work_dir=str(tmp_path / "work"), out_dir=str(tmp_path / "out"), show_name="Show", mkv_title_naming="")


def container_properties(file: Path) -> dict:
    out = subprocess.run([get_executable("mkvmerge"), "-J", str(file)], capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)["container"]["properties"]


def tag_names(file: Path) -> list[str]:
    out = subprocess.run([get_executable("mkvextract"), str(file), "tags", "-"], capture_output=True, text=True, encoding="utf-8")
    return re.findall(r"<Name>(.+?)</Name>", out.stdout)


def test_lang_token_does_not_swallow_following_tokens():
    matches = [m.group(0) for m in TOKEN_REGEX.finditer("[#alang_ja#][#crc32#] #alang_en# - #slang_de#")]
    assert matches == ["#alang_ja#", "#alang_en#", "#slang_de#"]


def test_lang_token_next_to_crc(setup: Setup):
    setup.edit("out_name", "$show$ - $ep$ [$alang_ja$][$crc32$]")
    out = mux(Premux(sample))
    assert re.fullmatch(r"Show - 01 \[JA\]\[[0-9A-F]{8}\]\.mkv", out.name)


def test_dynamic_token_rename_overwrites_existing(setup: Setup):
    setup.edit("out_name", "$show$ - $ep$ [$vres$]")
    first = mux(Premux(sample))
    second = mux(Premux(sample))
    assert first == second
    assert [p.name for p in first.parent.iterdir()] == ["Show - 01 [1080p].mkv"]


def test_token_index_out_of_range(setup: Setup):
    with pytest.raises(LoggingException, match="only 1 track"):
        apply_dynamic_tokens("#alang_jpn_3#", sample, True)


def test_backslash_in_values(setup: Setup):
    setup.edit("show_name", "AC\\DC")
    setup.edit("mkv_title_naming", "$show$ - $ep$")
    assert output_names()[1] == "AC\\DC - 01"


def test_dynamic_tokens_in_title(setup: Setup):
    setup.edit("mkv_title_naming", "$show$ - $ep$ [$vres$ $alang$]")
    out = mux(Premux(sample))
    assert container_properties(out)["title"] == "Show - 01 [1080p JA]"


def test_crc_in_title_raises(setup: Setup):
    setup.edit("mkv_title_naming", "$show$ [$crc32$]")
    with pytest.raises(LoggingException):
        output_names()


@pytest.mark.parametrize(
    "episode_title, expected",
    [("Goodbye.", "Show - 01 - Goodbye."), ("Ca$h$ Rules", "Show - 01 - Ca$h$ Rules"), ("Prologue []", "Show - 01 - Prologue []"), ("", "Show - 01")],
)
def test_episode_titles_are_not_cleaned(setup: Setup, episode_title: str, expected: str):
    setup.edit("out_name", "$show$ - $ep$ - $title$")
    setup.edit("mkv_title_naming", "$show$ - $ep$ - $title$")
    tmdb = TmdbConfig(1, write_ids=False, write_date=False)
    tmdb.get_media_meta = lambda: MediaMetadata(1, 0, "", "")  # type: ignore[method-assign]
    tmdb.get_episode_meta = lambda num: EpisodeMetadata(episode_title, "", "", "", episode_title)  # type: ignore[method-assign]
    assert output_names(tmdb) == (expected, expected)


def test_branding(setup: Setup):
    out = mux(Premux(sample))
    assert "muxtools" in container_properties(out)["muxing_application"]


def test_anonymize(setup: Setup):
    out = mux(Premux(sample), anonymize=True)
    props = container_properties(out)
    assert not props.get("muxing_application")
    assert not props.get("writing_application")
    assert "date_utc" not in props

    names = tag_names(out)
    assert "BPS" in names and "NUMBER_OF_BYTES" in names
    assert "_STATISTICS_WRITING_APP" not in names and "_STATISTICS_WRITING_DATE_UTC" not in names


def test_clean_work_dirs(setup: Setup, tmp_path: Path):
    setup.edit("clean_work_dirs", True)
    out = mux(Premux(sample))
    assert out.exists()
    assert not (tmp_path / "work").exists()


def test_clean_work_dirs_keeps_output_inside_workdir(setup: Setup, tmp_path: Path):
    setup.edit("clean_work_dirs", True)
    setup.edit("out_dir", str(tmp_path / "work" / "out"))
    out = mux(Premux(sample))
    assert out.exists()
