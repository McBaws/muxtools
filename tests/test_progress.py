import sys
import zlib
from pathlib import Path

import pytest

from muxtools import Setup, get_executable
from muxtools.utils.files import get_crc32
from muxtools.utils.subprogress import ProgressBarConfig, run_cmd_pb, run_mkvtoolnix_pb


@pytest.fixture(autouse=True)
def setup(tmp_path: Path) -> Setup:
    return Setup("01", work_dir=str(tmp_path / "work"))


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_failure_output_is_printed(capsys: pytest.CaptureFixture[str]):
    code = run_cmd_pb(python("print('10%'); print('something broke'); raise SystemExit(3)"), True, ProgressBarConfig("Testing..."))

    assert code == 3
    assert "something broke" in capsys.readouterr().out


def test_success_output_stays_hidden(capsys: pytest.CaptureFixture[str]):
    assert run_cmd_pb(python("print('50%'); print('all good')"), True, ProgressBarConfig("Testing...")) == 0
    assert "all good" not in capsys.readouterr().out


def test_multiple_success_codes(capsys: pytest.CaptureFixture[str]):
    code = run_cmd_pb(python("print('a warning'); raise SystemExit(1)"), True, ProgressBarConfig("Testing...", success_return=[0, 1]))

    assert code == 1
    assert "a warning" not in capsys.readouterr().out


def test_all_output_is_read(capsys: pytest.CaptureFixture[str]):
    run_cmd_pb(python("import sys\nfor i in range(2000): print(f'line {i}')\nsys.stdout.flush()"), False)

    assert "line 1999" in capsys.readouterr().out


def test_mkvtoolnix_failure_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    args = [get_executable("mkvmerge"), "-o", str(tmp_path / "out.mkv"), str(tmp_path / "missing.mkv")]

    assert run_mkvtoolnix_pb(args, "Muxing...") == 2
    assert "missing.mkv" in capsys.readouterr().out


def test_crc32(tmp_path: Path):
    file = tmp_path / "data.bin"
    data = bytes(range(256)) * 4096
    file.write_bytes(data)

    expected = "%08X" % (zlib.crc32(data) & 0xFFFFFFFF)
    assert get_crc32(file) == expected
    assert get_crc32(file, show_progress=False) == expected
