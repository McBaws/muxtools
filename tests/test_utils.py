from muxtools import ensure_path, Setup, get_workdir, GlobSearch
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


def test_empty_glob_search():
    with pytest.raises(Exception, match="did not find any files"):
        ensure_path(GlobSearch("nothing_matches_this_*.xyz", dir=test_dir), None)
