from pathlib import Path
from typing import Any, TYPE_CHECKING
from fractions import Fraction
from shlex import split as split_args
import re

from ..utils.files import make_output, create_tags_xml
from ..utils.glob import GlobSearch
from ..utils.types import PathLike, TrackType, FileMixin
from ..utils.files import ensure_path_exists, ensure_path
from ..utils.probe import ParsedFile
from ..utils.download import get_executable
from ..utils.language_util import standardize_tag
from ..utils.dataclass import kwargs_to_cli_args
from ..utils.log import error
from .token_handling import TOKEN_REGEX, apply_dynamic_tokens

if TYPE_CHECKING:
    from mkvinfo import MKVInfo

# fmt: off
__all__ = [
    "VideoTrack", "AudioTrack", "SubTrack",
    "VT", "AT", "ST",
    "Attachment", "Premux", "MkvTrack"
]


# fmt: on


CONTAINER_SUFFIXES = [".mkv", ".mka", ".mks", ".mk3d", ".webm", ".mp4", ".m4v", ".m4a", ".mov", ".m2ts", ".mts", ".ts", ".avi", ".ogm"]
MKVMERGE_TYPES = {TrackType.VIDEO: "VIDEO", TrackType.AUDIO: "AUDIO", TrackType.SUB: "SUBTITLES"}
TOKEN_TYPES = {TrackType.VIDEO: "v", TrackType.AUDIO: "a", TrackType.SUB: "s"}


def _mkvmerge_track_ids(file: Path, type: TrackType, info: "MKVInfo | None" = None) -> list[int]:
    """
    Returns the mkvmerge track IDs of all tracks of the given type in order.
    These can differ from the ffprobe indices, for example for TrueHD cores in m2ts files.

    :param info:    Already parsed mkvmerge output of the file, if available.
    """
    from mkvinfo import MKVInfo

    info = info or MKVInfo.from_file(file, mkvmerge=get_executable("mkvmerge"))
    return [track.id for track in info.tracks if track.type.name == MKVMERGE_TYPES[type]]


class _track:
    file: Path
    type: TrackType
    default: bool
    forced: bool
    name: str
    lang: str
    delay: int
    args: list[str]
    tags: dict[str, str] | None
    track: int
    _mkvinfo: "MKVInfo | None" = None

    def __init__(
        self,
        file: PathLike | GlobSearch | FileMixin,
        type: str | int | TrackType,
        name: str = "",
        lang: str = "",
        default: bool = True,
        forced: bool = False,
        delay: int | None = None,
        args: list[str] | None = None,
        tags: dict[str, str] | None = None,
        track: int = 0,
        **kwargs: Any,
    ) -> None:
        """
        :param file:        Filepath as string or Path object, or a file object like an AudioFile
        :param type:        TrackType enum, or int or string (1 = 'video', 2 = 'audio', 3 = 'sub')
        :param name:        The track name in the resulting mkv file
        :param lang:        The language tag for the track
        :param default:     Default flag
        :param forced:      Forced flag
        :param delay:       Container delay of track in ms.\n
                            If a file object is passed, its container delay is used unless a delay is passed explicitly (including 0).
        :param args:        Custom parameters as a list of strings to be applied to the track.\n
                            Track specific parameters should use the track ID 0, they are moved to the selected track automatically.
        :param tags:        Custom tags to be applied to the track. Defaults to the tags of a passed file object.
        :param track:       Which track of this type to take from the file, counted within the type and starting at 0.\n
                            Only this track is muxed. Chapters, attachments and tags of the file are skipped as well.
                            Use Premux to take more from a file.
        :param kwargs:      Any other custom parameters to be added to the track.\n
                            This follows the same patterns as the [dynamic dataclasses](https://muxtools.vodes.pw/guide/dynamic-dataclasses/).
        """
        if isinstance(file, FileMixin):
            delay = file.container_delay if delay is None else delay
            tags = file.tags if tags is None else tags
        if (isinstance(file, (list, tuple)) and len(file) > 1) or (isinstance(file, GlobSearch) and len(file.paths) > 1):
            raise error("Only a single file can be passed per track!", self)

        self.file = ensure_path_exists(file, self)
        self.default = default
        self.forced = forced
        self.name = name
        self.delay = delay or 0
        self.lang = lang
        self.type = type if isinstance(type, TrackType) else (TrackType(type) if isinstance(type, int) else TrackType[type.upper()])
        self.args = list(args) if args else []
        self.tags = tags
        self.track = track

        self.args.extend(kwargs_to_cli_args(**kwargs))

        # Containers can hold more than the wanted track, so only the selected one gets muxed
        self._track_id = 0
        self._selection_args: list[str] = []
        if self.type in MKVMERGE_TYPES and (track != 0 or self.file.suffix.lower() in CONTAINER_SUFFIXES):
            ids = _mkvmerge_track_ids(self.file, self.type, self._mkvinfo)
            if track >= len(ids):
                raise error(f"'{self.file.name}' does not have a {self.type.name.lower()} track {track}! ({len(ids)} found)", self)
            self._track_id = ids[track]
            selection = {
                TrackType.VIDEO: ["--video-tracks", str(self._track_id), "--no-audio", "--no-subtitles"],
                TrackType.AUDIO: ["--audio-tracks", str(self._track_id), "--no-video", "--no-subtitles"],
                TrackType.SUB: ["--subtitle-tracks", str(self._track_id), "--no-video", "--no-audio"],
            }
            self._selection_args = [*selection[self.type], "--no-buttons", "--no-chapters", "--no-attachments"]

    def _own_track_tokens(self, name: str) -> str:
        """Makes tokens without a type or index refer to the selected track instead of the first track of the file."""

        def replace(match: re.Match[str]) -> str:
            if match.group("type") or match.group("num") is not None or match.group("lang"):
                return match.group(0)
            token = match.group(0)
            return f"{token[0]}{TOKEN_TYPES[self.type]}{match.group('prop')}_{self.track}{token[-1]}"

        return TOKEN_REGEX.sub(replace, name)

    def mkvmerge_args(self) -> list[str]:
        filepath = str(self.file.resolve())
        if self.type == TrackType.ATTACHMENT:
            is_font = self.file.suffix.lower() in [".ttf", ".otf", ".ttc", ".otc"]
            if not is_font and not self.lang:
                raise ValueError("Please specify a mimetype for the attachments if they're not fonts!")
            args = ["--attachment-mime-type", self.lang or "application/x-truetype-font"]
            if self.name:
                args.extend(["--attachment-name", self.name])

            args.extend(["--attach-file", filepath])
            return args

        elif self.type == TrackType.MKV:
            if self.args:
                return [*self.args, filepath]
            return [filepath]
        elif self.type == TrackType.CHAPTERS:
            return ["--chapters", filepath]

        tid = self._track_id
        name = apply_dynamic_tokens(self._own_track_tokens(self.name), self.file, False, self.lang if self.lang else None)
        args = [*self._selection_args, "--no-global-tags", "--track-name", f"{tid}:{name}"]

        if self.tags and not all([not bool(v) for _, v in self.tags.items()]):
            tags_file = make_output(self.file, "xml", "_tags", temp=True)
            create_tags_xml(tags_file, self.tags)
            args.extend(["--tags", f"{tid}:{str(tags_file)}"])

        if self.lang:
            args.extend(["--language", f"{tid}:{standardize_tag(self.lang)[1]}"])
        if self.delay:
            args.extend(["--sync", f"{tid}:{self.delay}"])
        args.extend(
            [
                "--default-track-flag",
                f"{tid}:{'yes' if self.default else 'no'}",
                "--forced-display-flag",
                f"{tid}:{'yes' if self.forced else 'no'}",
            ]
        )
        if self.args:
            args.extend([re.sub(r"^0:", f"{tid}:", arg) for arg in self.args] if tid else self.args)
        args.append(filepath)
        return args


class VideoTrack(_track):
    """
    _track object with VIDEO type preselected and japanese language default

    :param timecode_file:       Pass a path for proper vfr playback if needed.
    :param crop:                Container based cropping with (horizontal, vertical) or (left, top, right, bottom).
                                Will crop the same on all sides if passed a single integer.

    See `_track` for the other parameters.
    """

    def __init__(
        self,
        file: PathLike | GlobSearch | FileMixin,
        name: str = "",
        lang: str = "ja",
        default: bool = True,
        forced: bool = False,
        delay: int | None = None,
        timecode_file: PathLike | GlobSearch | None = None,
        crop: int | tuple[int, int] | tuple[int, int, int, int] | None = None,
        args: list[str] | None = None,
        tags: dict[str, str] | None = None,
        track: int = 0,
        **kwargs: Any,
    ) -> None:
        path = ensure_path(file, self)
        args = list(args) if args else []
        if timecode_file is not None:
            args.extend(["--timestamps", f"0:{ensure_path_exists(timecode_file, self).resolve()}"])
        if crop:
            if isinstance(crop, int):
                croplist = [crop] * 4
            elif len(crop) == 2:
                croplist = list[int](crop) * 2
            else:
                croplist = list[int](crop)
            args.extend(["--cropping", f"0:{croplist[0]},{croplist[1]},{croplist[2]},{croplist[3]}"])

        # Check for SAR in bitstream and get new display-width / height params for mkvmerge
        if "--display-dimensions" not in args and "display_dimensions" not in kwargs:
            parsed = ParsedFile.from_file(path, self, False)
            self._mkvinfo = parsed.raw_mkvmerge
            video = parsed.find_tracks(type=TrackType.VIDEO, relative_id=track, caller=self)
            if not video:
                raise error(f"File '{path.name}' does not have a video track {track}!", self)
            if not video[0].raw_ffprobe.width or not video[0].raw_ffprobe.height:
                raise error(f"Video track {track} in file '{path.name}' does not have valid dimensions.", self)

            if video[0].raw_ffprobe.sample_aspect_ratio:
                if (sar_fraction := Fraction(str(video[0].raw_ffprobe.sample_aspect_ratio).replace(":", "/"))) != 1:
                    new_display_sizes = sar_fraction * video[0].raw_ffprobe.width / video[0].raw_ffprobe.height
                    kwargs.update(display_dimensions=f"0:{new_display_sizes.numerator}x{new_display_sizes.denominator}")

        super().__init__(file, TrackType.VIDEO, name, lang, default, forced, delay, args, tags, track, **kwargs)


class AudioTrack(_track):
    """
    _track object with AUDIO type preselected and japanese language default

    See `_track` for the parameters.
    """

    def __init__(
        self,
        file: PathLike | GlobSearch | FileMixin,
        name: str = "",
        lang: str = "ja",
        default: bool = True,
        forced: bool = False,
        delay: int | None = None,
        args: list[str] | None = None,
        tags: dict[str, str] | None = None,
        track: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(file, TrackType.AUDIO, name, lang, default, forced, delay, args, tags, track, **kwargs)


class Attachment(_track):
    """
    pseudo _track object for attachments

    :param file:        The file to attach
    :param mimetype:    Mimetype of the file. Only optional for fonts.
    :param name:        Name of the attachment in the mkv. Defaults to the filename.
    """

    def __init__(self, file: str | Path, mimetype: str = "", name: str = "") -> None:
        super().__init__(ensure_path(file, self), TrackType.ATTACHMENT, lang=mimetype, name=name)


class SubTrack(_track):
    """
    _track object with SUB type preselected and english language default

    Takes a single file. To combine multiple subtitle files, merge them beforehand with `SubFile.merge`.

    See `_track` for the parameters.
    """

    def __init__(
        self,
        file: PathLike | GlobSearch | FileMixin,
        name: str = "",
        lang: str = "en",
        default: bool = True,
        forced: bool = False,
        delay: int | None = None,
        args: list[str] | None = None,
        tags: dict[str, str] | None = None,
        track: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(file, TrackType.SUB, name, lang, default, forced, delay, args, tags, track, **kwargs)


class Premux(_track):
    def __init__(
        self,
        file: PathLike | GlobSearch,
        video: int | list[int] | None = -1,
        audio: int | list[int] | None = -1,
        subtitles: int | list[int] | None = -1,
        keep_attachments: bool = True,
        keep_chapters: bool = True,
        mkvmerge_args: str | list[str] = "--no-global-tags",
        assume_absolute: bool = False,
    ) -> None:
        """
        Custom Track object to arbitrarily grab tracks from an existing file.

        For all track params:
        `None` means there won't be any chosen and `-1` means all will be chosen.
        You can also specify a single or multiple *relative* track numbers to choose any.

        :param video:               Video Track(s) to choose
        :param audio:               Audio Track(s) to choose
        :param subtitles:           Subtitle Track(s) to choose
        :param keep_attachments:    Whether to keep attachments from the file. Fonts for example.
        :param keep_chapters:       Whether or not you want to keep existing chapters in the file.
        :param mkvmerge_args:       Any other args you may want to pass.
        :param assume_absolute:     Assume that the track numbers passed were already absolute mkvmerge track IDs to begin with.
                                    If False it will simply get absolute numbers derived from the relative ones.
        """
        path = ensure_path_exists(file, self)
        args: list[str] = []
        for selected, track_type, flag, none_flag in [
            (video, TrackType.VIDEO, "-d", "-D"),
            (audio, TrackType.AUDIO, "-a", "-A"),
            (subtitles, TrackType.SUB, "-s", "-S"),
        ]:
            if selected is None:
                args.append(none_flag)
                continue
            if selected == -1:
                continue
            if not isinstance(selected, list):
                selected = [selected]
            if not selected:
                raise error(f"Empty list passed for the {track_type.name.lower()} tracks. Use None to skip all of them.", self)

            if not assume_absolute:
                ids = _mkvmerge_track_ids(path, track_type)
                if missing := [num for num in selected if num >= len(ids)]:
                    raise error(f"'{path.name}' does not have {track_type.name.lower()} track(s) {missing}! ({len(ids)} found)", self)
                selected = [ids[num] for num in selected]
            args.extend([flag, ",".join(str(i) for i in selected)])

        if not keep_attachments:
            args.append("-M")

        if not keep_chapters:
            args.append("--no-chapters")

        mkvmerge_args = split_args(mkvmerge_args.strip()) if isinstance(mkvmerge_args, str) else mkvmerge_args
        super().__init__(path, TrackType.MKV, args=args + mkvmerge_args)


VT = VideoTrack
AT = AudioTrack
ST = SubTrack
MkvTrack = Premux
