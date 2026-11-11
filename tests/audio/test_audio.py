from muxtools import ensure_path, Setup, get_workdir, do_audio, get_executable, Opus, Sox, FFMpeg, VideoMeta, FLAC, AudioFile
from muxtools import FF_FLAC, CustomPreprocessor, Downmix, Loudnorm, Resample
from muxtools.utils.env import communicate_stdout
from muxtools.audio.audioutils import ensure_valid_in
from muxtools.utils.types import ValidInputType
from time import sleep
from shutil import rmtree, copy
import pytest
import logging
import re

test_dir = ensure_path(__file__, None).parent.parent
sample_file_aac = test_dir / "test-data" / "audio" / "aac_source.m4a"
sample_file_flac = test_dir / "test-data" / "audio" / "flac_source.flac"
sample_file_wav = test_dir / "test-data" / "audio" / "wav_source.wav"
sample_file_thd_fake24 = test_dir / "test-data" / "audio" / "thd_source_fake24.thd"
sample_file_eac3_mkv = test_dir / "test-data" / "sample-files" / "H265-Opus-EAC3-sample.mkv"


def get_md5_for_stream(file, codec: str | None = None) -> str:
    ffmpeg = get_executable("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(file), "-map", "0:a:0", *(["-c:a", codec] if codec else []), "-f", "md5", "-"]
    code, out = communicate_stdout(args)
    if code != 0:
        raise RuntimeError(f"Failed to get md5 for stream in file: {str(file)}")
    return out.split("=")[1].strip()


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


def test_lossy_input_no_encode():
    out = do_audio(sample_file_aac)
    assert get_md5_for_stream(out.file) == "3fe4c6674cc3b2ffc3ba3247c91ed622"


@pytest.mark.parametrize(
    "kwargs",
    [dict(), dict(encoder=None), dict(trimmer=None), dict(trimmer=None, encoder=None), dict(trims=(24, None))],
    ids=["defaults", "no-encoder", "no-trimmer", "no-trimmer-no-encoder", "trimmed"],
)
def test_do_audio_output(kwargs: dict) -> None:
    output = get_workdir() / "custom_output"

    out = do_audio(sample_file_aac, output=output, **kwargs)

    assert out.file.with_suffix("") == output.resolve()
    assert out.file.exists()


def test_do_audio_output_dir() -> None:
    output = get_workdir() / "out"
    output.mkdir()

    out = do_audio(sample_file_aac, output=output)

    assert out.file.with_suffix("") == (output / "aac_source").resolve()
    assert out.file.exists()


@pytest.mark.parametrize("source", [sample_file_aac, sample_file_flac], ids=["lossy", "lossless"])
def test_do_audio_list(source) -> None:
    single = do_audio(source, encoder=None)
    out = do_audio([source, source], encoder=None, output=get_workdir() / "concat")

    assert out.file.stem == "concat"
    assert out.duration == single.duration * 2


@pytest.mark.parametrize("preserve_delay", [False, True])
def test_lossy_multi_trim_starts_at_zero(preserve_delay: bool) -> None:
    source = AudioFile.from_file(sample_file_aac, None)
    source.container_delay = 123

    out = FFMpeg.Trimmer(trim=[(0, 24), (24, 48)], preserve_delay=preserve_delay).trim_audio(source)

    assert out.file.exists()
    assert out.container_delay == (123 if preserve_delay else 0)


@pytest.mark.parametrize("sample_file", [sample_file_flac, sample_file_wav])
def test_sox_bounded_trim(sample_file) -> None:
    out = Sox(trim=(1, 24)).trim_audio(sample_file)

    assert get_md5_for_stream(out.file) == "e6932cd8e59676a2e5c69246bb2cff77"


@pytest.mark.parametrize("sample_file", [sample_file_flac, sample_file_wav])
@pytest.mark.parametrize("preserve_delay", [False, True])
def test_sox_multi_trim_with_padding(sample_file, preserve_delay: bool) -> None:
    meta = VideoMeta.from_json(test_dir / "test-data" / "input" / "vigilantes_s01e01.json")
    source = AudioFile.from_file(sample_file, None)
    source.container_delay = 123

    out = Sox(trim=[(-2, 3), (12, 24), (3, 6)], num_frames=len(meta.pts), preserve_delay=preserve_delay).trim_audio(source)

    assert get_md5_for_stream(out.file) == "c1739f08d9e17219ed8f932943bb4a8d"
    assert out.container_delay == (123 if preserve_delay else 0)
    assert out.source == source.source


@pytest.mark.parametrize("preserve_delay", [False, True])
def test_lossy_multi_trim_keeps_delay(preserve_delay: bool) -> None:
    source = AudioFile.from_file(sample_file_aac, None)
    source.container_delay = 123

    single = FFMpeg.Trimmer(trim=(24, 48), preserve_delay=preserve_delay).trim_audio(source)
    multi = FFMpeg.Trimmer(trim=[(24, 48), (72, 96)], preserve_delay=preserve_delay).trim_audio(source)

    # Both start at the same frame so they need the same delay
    assert multi.container_delay == single.container_delay
    assert (single.container_delay > 100) == preserve_delay


@pytest.mark.parametrize("preserve_delay", [False, True])
def test_extractor_container_delay(preserve_delay: bool) -> None:
    # The EAC3 track in this sample starts 13 ms in
    out = FFMpeg.Extractor(track=1, preserve_delay=preserve_delay).extract_audio(sample_file_eac3_mkv)

    assert out.container_delay == (13 if preserve_delay else 0)


# Some of these tests randomly fail on some github runners and I'm not sure why.
# Only a concern when both python versions and repeated fails occur.
@pytest.mark.xfail
def test_lossy_input(caplog):
    out = do_audio(sample_file_aac, encoder=Opus())

    # Prints a danger log for reencoding lossy audio
    assert len([record for record in caplog.get_records("call") if record.levelname == "DANGER"]) == 1

    assert get_md5_for_stream(out.file) == "f5c4b6e88bb590ff94e15836ebfbd5f7"


@pytest.mark.xfail
def test_flac_input():
    out = do_audio(sample_file_flac, encoder=Opus())
    assert get_md5_for_stream(out.file) == "f2dac56cf04fce1d411d84ebb9cc9b9f"


@pytest.mark.xfail
def test_flac_sox_trim():
    meta = VideoMeta.from_json(test_dir / "test-data" / "input" / "vigilantes_s01e01.json")

    out = do_audio(sample_file_flac, trims=(-24, None), num_frames=len(meta.pts), timesource=meta, trimmer=Sox(), encoder=Opus())
    assert get_md5_for_stream(out.file) == "e4850767705690b94a8771de69896402"


@pytest.mark.xfail
def test_flac_ffmpeg_trim():
    meta = VideoMeta.from_json(test_dir / "test-data" / "input" / "vigilantes_s01e01.json")

    out = do_audio(sample_file_flac, trims=(24, None), timesource=meta, trimmer=FFMpeg.Trimmer(), encoder=Opus())
    assert get_md5_for_stream(out.file) == "3866b432a30580b87d8ce4369d1ddcce"


def test_depth_detection(caplog):
    caplog.set_level(logging.DEBUG)

    out = do_audio(sample_file_thd_fake24, encoder=FLAC(threads=1))

    assert len([record for record in caplog.get_records("call") if "padded 24" in record.message]) == 1
    assert len([record for record in caplog.get_records("call") if "truncated to 16" in record.message]) == 1

    assert get_md5_for_stream(out.file) == "52219678c3ac04dcd34e15d8a55e35df"


def test_opus_no_extractor(caplog):
    caplog.set_level(logging.DEBUG)

    # Should encode file as is, 192 kbps default for stereo
    Opus().encode_audio(sample_file_wav)
    assert len([record for record in caplog.get_records("call") if "Encoding 'wav_source' to Opus (192 kbps)" in record.message]) == 1
    assert len([record for record in caplog.get_records("call") if "Piping audio" in record.message]) == 0

    # Should pipe from ffmpeg to have flac input because THD is not supported, 320 kbps default for 5.1
    Opus().encode_audio(sample_file_thd_fake24)
    assert len([record for record in caplog.get_records("call") if "Encoding 'thd_source_fake24' to Opus (320 kbps)" in record.message]) == 1
    assert len([record for record in caplog.get_records("call") if "Piping audio" in record.message]) == 1


def _duration(file) -> float:
    ffprobe = get_executable("ffprobe")
    code, out = communicate_stdout([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(file)])
    if code != 0:
        raise RuntimeError(f"Failed to get duration of {str(file)}")
    return float(out.strip())


def test_concat_mismatched_formats():
    out = FFMpeg.Concat([AudioFile(sample_file_aac), AudioFile(sample_file_flac)]).concat_audio()

    track = out.get_trackinfo()
    assert track.codec_name == "flac"
    assert track.bit_depth == 24
    assert _duration(out.file) == pytest.approx(_duration(sample_file_aac) + _duration(sample_file_flac), abs=0.1)


def test_concat_with_cover_art():
    ffmpeg = get_executable("ffmpeg")
    cover = get_workdir() / "cover.png"
    with_cover = get_workdir() / "cover.flac"
    assert (
        communicate_stdout([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=32x32", "-frames:v", "1", str(cover)])[0]
        == 0
    )
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(sample_file_flac), "-i", str(cover)]
    args.extend(["-map", "0:a", "-map", "1:v", "-c", "copy", "-disposition:v", "attached_pic", str(with_cover)])
    assert communicate_stdout(args)[0] == 0
    assert AudioFile(with_cover).get_containerinfo().nb_streams == 2

    out = FFMpeg.Concat([AudioFile(with_cover), AudioFile(with_cover)]).concat_audio()

    assert out.get_containerinfo().nb_streams == 1
    without_cover = FFMpeg.Concat([AudioFile(sample_file_flac), AudioFile(sample_file_flac)]).concat_audio()
    assert get_md5_for_stream(out.file) == get_md5_for_stream(without_cover.file)


@pytest.mark.parametrize("name", ["テスト.m4a", "café.m4a"])
def test_lossy_multi_trim_non_ascii_name(name: str):
    source = get_workdir() / name
    copy(sample_file_aac, source)

    out = FFMpeg.Trimmer(trim=[(0, 24), (48, 72)]).trim_audio(source)

    assert out.file.exists()
    assert _duration(out.file) == pytest.approx(2, abs=0.1)


def test_custom_preprocessor_string_args():
    assert CustomPreprocessor(args="-ac 1").get_args() == ["-ac", "1"]

    out = FF_FLAC(preprocess=CustomPreprocessor(args="-ac 1")).encode_audio(sample_file_flac)
    assert out.get_trackinfo().raw_ffprobe.channels == 1


def test_resample_only_sample_rate():
    out = FF_FLAC(preprocess=Resample(depth=None, sample_rate=44100)).encode_audio(sample_file_flac)

    track = out.get_trackinfo()
    assert track.raw_ffprobe.sample_rate == 44100
    assert track.bit_depth == 24


def test_loudnorm_zero_offset():
    loudnorm = Loudnorm(offset=0.0)
    loudnorm.measurements = Loudnorm.Measurements(-20.0, 5.0, -1.0, -30.0, 1.5)

    assert ":offset=0.0:" in (loudnorm.get_filter() or "")


def _integrated_loudness(file) -> float:
    ffmpeg = get_executable("ffmpeg")
    _, out = communicate_stdout([ffmpeg, "-hide_banner", "-nostats", "-i", str(file), "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"])
    return float(re.findall(r"I:\s+(-?\d+\.\d+) LUFS", out)[-1])


def test_loudnorm_measures_after_downmix():
    # Most of the energy is in the center channel so the downmix changes the loudness a lot
    ffmpeg = get_executable("ffmpeg")
    surround = get_workdir() / "surround.flac"
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=c=pink:a=0.3:d=20:r=48000"]
    args.extend(["-af", "pan=5.1|FL=0.1*c0|FR=0.1*c0|FC=c0|LFE=0*c0|BL=0.05*c0|BR=0.05*c0", "-c:a", "flac", str(surround)])
    assert communicate_stdout(args)[0] == 0

    out = FF_FLAC(preprocess=[Downmix(), Loudnorm(), Resample()]).encode_audio(surround)

    assert out.get_trackinfo().raw_ffprobe.channels == 2
    assert _integrated_loudness(out.file) == pytest.approx(-24, abs=0.5)


def _generate(output, *args: str):
    ffmpeg = get_executable("ffmpeg")
    code, out = communicate_stdout([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, str(output)])
    if code != 0:
        raise RuntimeError(f"Failed to generate test file: {out}")
    return output


def _sample_info(file) -> tuple[str, str | None, int | None]:
    track = AudioFile(file).get_trackinfo()
    return track.codec_name, track.raw_ffprobe.sample_fmt, track.bit_depth


@pytest.mark.parametrize("codec", ["pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"])
def test_pcm_keeps_sample_format(codec: str):
    source = _generate(get_workdir() / f"{codec}.mka", "-f", "lavfi", "-i", "sine=r=48000:d=2", "-c:a", codec)

    extracted = FFMpeg.Extractor(skip_analysis=True).extract_audio(source)
    assert _sample_info(extracted.file) == _sample_info(source)

    prepared = ensure_valid_in(AudioFile(source), supports_pipe=False, valid_type=ValidInputType.RF64)
    assert _sample_info(prepared.file) == _sample_info(source)


@pytest.mark.parametrize("container", ["wav", "mka"])
def test_flac_converts_float(container: str):
    source = _generate(get_workdir() / f"float.{container}", "-f", "lavfi", "-i", "sine=r=48000:d=2", "-c:a", "pcm_f32le")

    out = FLAC(preprocess=None).encode_audio(source)
    assert _sample_info(out.file) == ("flac", "s32", 24)


def test_depth_detection_keeps_real_bits():
    # 16 bit padded to 24 for most of it but the last second is real 24 bit
    noise = "anoisesrc=a=0.2:r=48000:d={}"
    mixed = _generate(
        get_workdir() / "mixed.flac",
        *["-f", "lavfi", "-i", noise.format(20), "-f", "lavfi", "-i", noise.format(1)],
        *["-filter_complex", "[0:a]aformat=s16,aformat=s32[a];[1:a]aformat=s32[b];[a][b]concat=n=2:v=0:a=1"],
        *["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"],
    )

    out = FFMpeg.Extractor().extract_audio(mixed)
    assert _sample_info(out.file)[2] == 24
    assert get_md5_for_stream(out.file, "pcm_s32le") == get_md5_for_stream(mixed, "pcm_s32le")


def test_depth_detection_ignores_silence(caplog):
    caplog.set_level(logging.DEBUG)
    # 16 bit padded to 24 with long digital silence
    padded = _generate(
        get_workdir() / "padded.flac",
        *["-f", "lavfi", "-i", "anoisesrc=a=0.2:r=48000:d=5", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=15"],
        *["-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1,aformat=s16,aformat=s32"],
        *["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"],
    )

    out = FFMpeg.Extractor().extract_audio(padded)
    assert len([record for record in caplog.get_records("call") if "truncated to 16" in record.message]) == 1
    assert _sample_info(out.file)[2] == 16
    assert get_md5_for_stream(out.file, "pcm_s32le") == get_md5_for_stream(padded, "pcm_s32le")
