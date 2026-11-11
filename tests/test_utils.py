from muxtools import ensure_path, Setup, get_workdir, get_executable, get_timemeta_from_video
from muxtools.utils.env import communicate_stdout
from time import sleep
from shutil import rmtree
import pytest

test_dir = ensure_path(__file__, None).parent


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


def test_timemeta_same_stem():
    ffmpeg = get_executable("ffmpeg")
    videos = []
    for folder, seconds in [("a", 1), ("b", 2)]:
        video = get_workdir() / folder / "episode.mkv"
        video.parent.mkdir()
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=d={seconds}:r=24:s=64x64", "-c:v", "libx264", str(video)]
        assert communicate_stdout(args)[0] == 0
        videos.append(video)

    first = get_timemeta_from_video(videos[0])
    second = get_timemeta_from_video(videos[1])

    assert len(second.pts) > len(first.pts)
    assert second.source == str(videos[1].resolve())
    # Reused if it's the same file
    assert get_timemeta_from_video(videos[1]) == second
