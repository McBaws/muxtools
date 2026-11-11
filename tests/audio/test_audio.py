from muxtools import ensure_path, Setup, get_workdir, do_audio, get_executable, Opus, Sox, FFMpeg, VideoMeta, FLAC, AudioFile
from muxtools.utils.env import communicate_stdout
from time import sleep
from shutil import rmtree
import pytest
import logging

test_dir = ensure_path(__file__, None).parent.parent
sample_file_aac = test_dir / "test-data" / "audio" / "aac_source.m4a"
sample_file_flac = test_dir / "test-data" / "audio" / "flac_source.flac"
sample_file_wav = test_dir / "test-data" / "audio" / "wav_source.wav"
sample_file_thd_fake24 = test_dir / "test-data" / "audio" / "thd_source_fake24.thd"


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


def _generate(output, *args: str):
    ffmpeg = get_executable("ffmpeg")
    code, out = communicate_stdout([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, str(output)])
    if code != 0:
        raise RuntimeError(f"Failed to generate test file: {out}")
    return output


def _sample_info(file) -> tuple[str, str | None, int | None]:
    track = AudioFile(file).get_trackinfo()
    return track.codec_name, track.raw_ffprobe.sample_fmt, track.bit_depth


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
