import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from muxtools import Setup, TmdbConfig, TMDBOrder
from muxtools.utils.log import LoggingException


class FakeResponse:
    def __init__(self, data: Any, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self) -> Any:
        return self._data


class FakeTMDB:
    """Answers requests by the first route that is part of the url and records every request."""

    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        return next(response for route, response in self.routes.items() if route in url)


def season(*names: str, still_path: str | None = "/still.jpg") -> FakeResponse:
    return FakeResponse({"episodes": [{"name": name, "still_path": still_path} for name in names]})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    Setup("01", work_dir=str(tmp_path / "work"))

    def install(routes: dict[str, FakeResponse]) -> FakeTMDB:
        tmdb = FakeTMDB(routes)
        module = types.ModuleType("niquests")
        module.get = tmdb.get  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "niquests", module)
        return tmdb

    return install


@pytest.mark.parametrize("num, offset", [(0, 0), (1, -1), (5, -6)])
def test_episode_numbers_below_one_raise(fake, num: int, offset: int):
    fake({"/season/1": season("E1", "E2", "E3")})
    with pytest.raises(LoggingException, match="1 or higher"):
        TmdbConfig(1, offset=offset).get_episode_meta(num)


def test_offset_maps_to_episode(fake):
    fake({"/season/1": season("E1", "E2", "E3")})
    assert TmdbConfig(1, offset=-1).get_episode_meta(3).title == "E2"


def test_null_ids_are_not_written(fake):
    fake({"external_ids": FakeResponse({"imdb_id": None, "tvdb_id": None}), "/tv/1": FakeResponse({"overview": "x"})})
    config = TmdbConfig(1)
    media = config.get_media_meta()
    assert media.imdb_id == "" and media.tvdb_id == 0

    tags = {simple.findtext("Name"): simple.findtext("String") for simple in ET.parse(config.make_xml(media)).getroot().iter("Simple")}
    assert tags == {"TMDB": "tv/1"}


def test_failed_external_ids_request_raises(fake):
    fake({"external_ids": FakeResponse({"status_message": "nope"}, 404), "/tv/1": FakeResponse({"overview": "x"})})
    with pytest.raises(LoggingException, match="External IDs"):
        TmdbConfig(1).get_media_meta()


def test_missing_still_skips_cover(fake):
    fake({"/season/1": season("E1", still_path=None)})
    assert TmdbConfig(1, write_cover=True).get_episode_meta(1).thumb_url == ""


def test_still_is_used_for_cover(fake):
    fake({"/season/1": season("E1")})
    assert TmdbConfig(1, write_cover=True).get_episode_meta(1).thumb_url == "https://image.tmdb.org/t/p/w780/still.jpg"


def test_changing_season_refetches(fake):
    tmdb = fake({"/season/1": season("S1E1"), "/season/2": season("S2E1")})
    config = TmdbConfig(1)
    assert config.get_episode_meta(1).title == "S1E1"
    assert config.get_episode_meta(1).title == "S1E1"
    assert len(tmdb.requests) == 1

    config.season = 2
    assert config.get_episode_meta(1).title == "S2E1"
    assert len(tmdb.requests) == 2


def test_episode_group_with_fewest_subgroups_is_used(fake):
    groups = [
        {"id": "many", "name": "Many", "type": int(TMDBOrder.PRODUCTION), "group_count": 5},
        {"id": "few", "name": "Few", "type": int(TMDBOrder.PRODUCTION), "group_count": 2},
    ]
    tmdb = fake(
        {
            "/episode_groups": FakeResponse({"results": groups}),
            "/episode_group/few": FakeResponse({"groups": [{"order": 1, "episodes": [{"name": "G1"}]}]}),
        }
    )
    assert TmdbConfig(1, order=TMDBOrder.PRODUCTION).get_episode_meta(1).title == "G1"
    assert "/episode_group/few" in tmdb.requests[-1][0]


def test_requests_have_a_timeout(fake):
    tmdb = fake({"/season/1": season("E1")})
    TmdbConfig(1).get_episode_meta(1)
    assert all(kwargs.get("timeout") for _, kwargs in tmdb.requests)
