import shutil
from pathlib import Path

import pytest

from muxtools import Setup, AudioFile, VideoFile, MuxingFile, AudioTrack, VideoTrack, SubTrack, Premux
from muxtools.utils.log import LoggingException

test_data = Path(__file__).parent / "test-data"
samples = test_data / "sample-files"
flac = test_data / "audio" / "flac_source.flac"
ass = test_data / "input" / "vigilantes_s01e01_en.ass"


@pytest.fixture(autouse=True)
def setup(tmp_path: Path) -> Setup:
    return Setup("01", work_dir=str(tmp_path / "work"), out_dir=str(tmp_path / "out"), mkv_title_naming="")


def test_file_object_delay_and_tags_are_used():
    audio = AudioFile(flac, container_delay=120, tags={"ENCODER_SETTINGS": "x"})
    track = AudioTrack(audio)

    assert track.delay == 120 and track.tags == {"ENCODER_SETTINGS": "x"}
    assert "0:120" in track.mkvmerge_args()
    assert VideoTrack(VideoFile(samples / "H264-10bit-FLAC-sample.mkv", container_delay=-42)).delay == -42


def test_explicit_delay_and_tags_win():
    track = AudioTrack(AudioFile(flac, container_delay=120, tags={"A": "1"}), delay=0, tags={"B": "2"})

    assert track.delay == 0 and track.tags == {"B": "2"}
    assert "--sync" not in track.mkvmerge_args()


def test_args_list_is_not_modified():
    shared = ["--compression", "0:none"]
    VideoTrack(samples / "H264-10bit-FLAC-sample.mkv", crop=8, args=shared)
    second = VideoTrack(samples / "H264-10bit-FLAC-sample.mkv", crop=8, args=shared)
    kwargs_list = ["--compression", "0:none"]
    AudioTrack(flac, args=kwargs_list, original_flag="0:yes")

    assert shared == ["--compression", "0:none"] and kwargs_list == ["--compression", "0:none"]
    assert second.mkvmerge_args().count("--cropping") == 1


def test_multiple_files_per_track_raise():
    with pytest.raises(LoggingException):
        SubTrack([ass, test_data / "input" / "atri-subkt" / "warning.ass"])

    assert SubTrack([ass]).file == ass.resolve()


def test_premux_uses_mkvmerge_track_ids():
    # ffprobe lists the TrueHD cores as separate streams here, so its subtitle indices are off by two
    premux = Premux(samples / "H264-THD-Atmos-PCM-DTS-HeadphoneX-sample.m2ts", video=None, audio=None, subtitles=[0, 1])

    assert premux.args == ["-D", "-A", "-s", "9,10", "--no-global-tags"]


def test_premux_empty_list_raises():
    with pytest.raises(LoggingException):
        Premux(samples / "H264-10bit-FLAC-sample.mkv", video=[])


def test_muxing_file_is_abstract():
    with pytest.raises(TypeError):
        MuxingFile(flac)  # type: ignore[abstract]


def test_missing_audio_file_raises(tmp_path: Path):
    with pytest.raises(LoggingException):
        AudioFile(tmp_path / "missing.wav")


def test_to_mka(tmp_path: Path):
    wav = Path(shutil.copy(test_data / "audio" / "wav_source.wav", tmp_path))
    audio = AudioFile(wav, container_delay=50)
    mka = audio.to_mka()

    assert not wav.exists()
    assert audio.file == mka and audio.container_delay == 0

    with pytest.raises(LoggingException):
        audio.to_mka()
