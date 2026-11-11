from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import cast
import logging

from ..utils.log import debug, error, info, warn
from ..utils.files import create_tags_xml


# https://github.com/Radarr/Radarr/blob/29ba6fe5563e737f0f87919e48f556e39284e6bb/src/NzbDrone.Common/Cloud/RadarrCloudRequestBuilder.cs#L31
# skill issue
API_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJhdWQiOiIxYTczNzMzMDE5NjFkMDNmOTdmODUzYTg3NmRkMTIxMiIsInN1YiI6IjU4NjRmNTkyYzNhMzY4MGFiNjAxNzUzNCIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.gh1BwogCCKOda6xj9FRMgAAj_RYKMMPC3oNlcBtlmwk"
BASE_URL = "https://api.themoviedb.org/3"
HEADERS = {"accept": "application/json", "Authorization": f"Bearer {API_KEY}"}
TIMEOUT = 60

logging.getLogger("urllib3.connectionpool").setLevel(logging.CRITICAL)


__all__ = ["TmdbConfig", "TitleTMDB", "TMDBOrder"]


class TMDBOrder(IntEnum):
    """
    TMDB Episode groups or orders. Don't think there is a universally agreed on name.\n
    This enum is for automatically selecting one with the given type.
    """

    ORIGINAL_AIR_DATE = 1
    """Original air date order"""
    ABSOLUTE = 2
    """Absolute numbering order"""
    DVD = 3
    """DVD order"""
    DIGITAL = 4
    """Digital order"""
    STORY_ARC = 5
    """Story Arc order"""
    PRODUCTION = 6
    """
    Production order, this is usually what contains the proper seasons now that
    TMDB mods decided to be weird.

    See https://www.themoviedb.org/tv/95479/discuss/64a5672ada10f0011cb49f99
    """
    TV = 7
    """TV order"""


@dataclass
class MediaMetadata:
    tmdb_id: int
    tvdb_id: int
    imdb_id: str
    summary: str

    release_date: str | None = None


@dataclass
class EpisodeMetadata:
    title: str
    release_date: str
    synopsis: str
    thumb_url: str
    title_sanitized: str


@dataclass
class TmdbConfig:
    """
    A simple configuration class for TMDB Usage in muxing.

    For episodes, the fetched title can be used in the `out_name` and `mkv_title_naming` templates of the Setup
    with the `$title$` and `$title_sanitized$` placeholders. The sanitized version is the one that is safe to use in filenames.

    :param id:                  TMDB Media ID. The numerical part in URLs like https://www.themoviedb.org/tv/82684/...
    :param season:              The number of the season. If given an order this will be the Nth subgroup in that order.
    :param movie:               Is this a movie?
    :param order:               Episode group/order enum or a string for an exact ID. Obviously not applicable to a movie.\n
                                If there are multiple groups of the given type, the one with the fewest subgroups is used.
    :param language:            The metadata language. Defaults to english.
                                This takes ISO 639-1 codes, optionally with a region like the default `en-US`.
                                See https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes
                                Be aware that some metadata might just not exist in your requested language. Check beforehand.

    :param offset:              Offset to apply to the current number used in the setup that will be matched to the TMDB number.\n
                                The resulting number has to be 1 or higher.

    :param write_ids:           Writes the IDs (IMDB, TVDB, TMDB) to their respective mkv tags if True
    :param write_date:          Writes the episode release date to the `DATE_RELEASED` mkv tag if True
    :param write_title:         Writes the episode title to the `DESCRIPTION` mkv tag if True. Not applicable to movies.
    :param write_cover:         Download episode thumbnail from TMDB to use as cover art attachment for the MKV.
                                Skipped with a warning if TMDB has no thumbnail for the episode.
    :param write_summary:       Writes the series summary/synopsis to the `SUMMARY` mkv tag if True
    :param write_synopsis:      Writes the individual episode synopsis to the `SYNOPSIS` mkv tag if True
    :param replace_spaces:      Replaces spaces in titles with dots if True and with whatever string you passed if a string.
    :param title_sanitization:  A dictionary of characters to replace in titles. Defaults to removing `<>:"/\\|?*` characters.
    """

    id: int
    season: int = 1
    movie: bool = False
    order: TMDBOrder | str | None = None
    language: str = "en-US"
    offset: int = 0

    write_ids: bool = True
    write_date: bool = True
    write_title: bool = False
    write_cover: bool = False
    write_summary: bool = False
    write_synopsis: bool = False
    replace_spaces: str | bool = False
    title_sanitization: dict[str, str] = field(default_factory=lambda: {'<>:"/\\|?*': ""})

    def needs_xml(self) -> bool:
        return self.write_ids or self.write_date or self.write_title or self.write_summary or self.write_synopsis

    def _request(self, url: str, name: str) -> dict:
        import niquests

        response = niquests.get(url, headers=HEADERS, timeout=TIMEOUT)

        if response.status_code is not None and response.status_code > 202:
            ex = error(f"{name} Request has failed! ({response.status_code})", self)
            debug(response.text or "")
            raise ex

        return response.json()

    def get_media_meta(self) -> MediaMetadata:
        media_type = "movie" if self.movie else "tv"
        mediajson = self._request(f"{BASE_URL}/{media_type}/{self.id}?language={self.language}", "Media Metadata")
        other_ids = self._request(f"{BASE_URL}/{media_type}/{self.id}/external_ids", "External IDs")

        # TMDB sends null for IDs it doesn't know
        return MediaMetadata(
            self.id,
            other_ids.get("tvdb_id") or 0,
            other_ids.get("imdb_id") or "",
            mediajson.get("overview", ""),
            mediajson.get("release_date", None) if self.movie else None,
        )

    def _order_id(self) -> str:
        if isinstance(self.order, str):
            return self.order

        assert self.order
        orders = self._request(f"{BASE_URL}/tv/{self.id}/episode_groups", "Episode Groups")["results"]
        if not orders:
            raise error("Could not find any episode groups/orders for this show.", self)
        filtered = [order for order in orders if order["type"] == self.order]
        if not filtered:
            raise error(f"Could not find an episode groups/orders for type {self.order.name}.", self)

        wanted_order = sorted(filtered, key=lambda it: it["group_count"])[0]
        info(f"Selecting episode group '{wanted_order['name']}'.", self)
        return str(wanted_order["id"])

    def get_episode_meta(self, num: int) -> EpisodeMetadata:
        # Refetch if any of the values deciding which episodes we get changed since the last call
        episodes_key = (self.id, self.season, self.order, self.language)
        if getattr(self, "_episodes_key", None) != episodes_key:
            if self.order:
                url = f"{BASE_URL}/tv/episode_group/{self._order_id()}?language={self.language}"
            else:
                url = f"{BASE_URL}/tv/{self.id}/season/{self.season}?language={self.language}"

            json_resp = self._request(url, "Episode Metadata")
            if "groups" in json_resp:
                groups = json_resp["groups"]
                try:
                    group = [group for group in groups if group["order"] == self.season][0]
                except:
                    raise error(f"Could not find subgroup for the number {self.season}.", self)
                self.episodes = group["episodes"]
            else:
                self.episodes = json_resp["episodes"]
            self._episodes_key = episodes_key

        # Negative indices would silently wrap around to the end of the season
        index = num + self.offset
        if index < 1:
            raise error(f"Episode {num:02} with an offset of {self.offset} doesn't map to a TMDB episode number of 1 or higher!", self)

        try:
            episode: dict = self.episodes[index - 1]
        except:
            raise error(f"Failed to find or parse episode {num:02}!", self)

        thumb_url = ""
        if self.write_cover:
            if still_path := episode.get("still_path"):
                thumb_url = f"https://image.tmdb.org/t/p/w780{still_path}"
            else:
                warn(f"TMDB has no thumbnail for episode {num:02}. Skipping the cover.", self)

        title = cast(str, episode.get("name", ""))
        if self.replace_spaces is True:
            title = title.replace(" ", ".")
        elif isinstance(self.replace_spaces, str):
            title = title.replace(" ", self.replace_spaces)

        sanitized_title = title
        for key, value in self.title_sanitization.items():
            for char in sanitized_title:
                if char in key:
                    sanitized_title = sanitized_title.replace(char, value)

        return EpisodeMetadata(
            title,
            episode.get("air_date", ""),
            episode.get("overview", ""),
            thumb_url,
            sanitized_title,
        )

    def make_xml(self, media: MediaMetadata, episode: EpisodeMetadata | None = None) -> Path:
        from ..utils.files import make_output

        tags = dict[str, str]()

        if self.write_title and episode:
            tags.update(DESCRIPTION=episode.title)
        if self.write_ids:
            if not self.movie and media.tvdb_id:
                tags.update(TVDB=str(media.tvdb_id))
            prefix = "movie/" if self.movie else "tv/"
            tags.update(TMDB=prefix + str(media.tmdb_id))
            if media.imdb_id:
                tags.update(IMDB=media.imdb_id)
        if self.write_date:
            if self.movie:
                if media.release_date:
                    tags.update(DATE_RELEASED=media.release_date)
            elif episode is not None:
                tags.update(DATE_RELEASED=episode.release_date)
        if self.write_summary:
            tags.update(SUMMARY=media.summary)
        if self.write_synopsis and not self.movie and episode is not None:
            tags.update(SYNOPSIS=episode.synopsis)

        outfile = make_output("tags", "xml")
        create_tags_xml(outfile, tags)

        return outfile


def TitleTMDB(
    id: int, season: int = 1, movie: bool = False, language: str = "en-US", offset: int = 0, order: TMDBOrder | str | None = None
) -> TmdbConfig:
    """
    Shortcut function to get a TMDB config that only fetches episode titles.

    The titles are only used for the `$title$` and `$title_sanitized$` placeholders in the Setup naming templates.
    No mkv tags or cover are written. Since titles are fetched per episode, this does nothing for movies.

    :param id:              TMDB Media ID. The numerical part in URLs like https://www.themoviedb.org/tv/82684/...
    :param season:          The number of the season. If given an order this will be the Nth subgroup in that order.
    :param movie:           Is this a movie?
    :param order:           Episode group/order enum or a string for an exact ID. Obviously not applicable to a movie.
    :param language:        The metadata language. Defaults to english.
                            This takes ISO 639-1 codes, optionally with a region like the default `en-US`.
                            See https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes
                            Be aware that some metadata might just not exist in your requested language. Check beforehand.

    :param offset:          Offset to apply to the current number used in the setup that will be matched to the TMDB number.
    """
    return TmdbConfig(
        id,
        season,
        movie,
        order,
        language,
        offset,
        write_ids=False,
        write_date=False,
        write_title=False,
        write_cover=False,
        write_summary=False,
        write_synopsis=False,
    )
