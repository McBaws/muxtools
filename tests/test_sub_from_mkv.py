from muxtools import Setup, get_workdir, ensure_path, SubFile, get_executable
from muxtools.utils.env import communicate_stdout
from shutil import rmtree
from time import sleep
import pytest

test_dir = ensure_path(__file__, None).parent


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


def test_from_mkv_twice_gives_separate_files() -> None:
    mks = get_workdir() / "subs.mks"
    args = [get_executable("mkvmerge"), "-o", str(mks), str(test_dir / "test-data" / "input" / "vigilantes_s01e01_en.ass")]
    assert communicate_stdout(args)[0] in (0, 1)

    first = SubFile.from_mkv(mks)
    line_count = len(first._read_doc().events)
    first.manipulate_lines(lambda lines: lines[:1])

    second = SubFile.from_mkv(mks)

    assert second.file != first.file
    # The edit made through the first one is still there, and the second one has the full track
    assert len(first._read_doc().events) == 1
    assert len(second._read_doc().events) == line_count
