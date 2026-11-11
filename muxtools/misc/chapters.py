from __future__ import annotations

from datetime import timedelta
from fractions import Fraction
from pathlib import Path
from typing import TypeVar, cast
from video_timestamps import TimeType, ABCTimestamps, FPSTimestamps
import re

from ..subtitle.sub import SubFile
from ..subtitle.basesub import _Line
from ..utils.log import error, info, warn, debug
from ..utils.glob import GlobSearch
from ..utils.download import get_executable
from ..utils.types import Chapter, LooseChapter, PathLike, TimeSourceT, TimeScaleT, TimeScale
from ..utils.parsing import parse_ogm, parse_xml
from ..utils.files import clean_temp_files, ensure_path_exists, ensure_path
from ..utils.env import get_temp_workdir, get_workdir, run_commandline, get_setup_attr
from ..utils.convert import format_timedelta, resolve_timesource_and_scale, timedelta_to_frame

__all__ = ["Chapters"]


class Chapters:
    chapters: list[Chapter]
    fps: Fraction | PathLike
    timestamps: ABCTimestamps

    def __init__(
        self,
        chapter_source: PathLike | GlobSearch | LooseChapter | list[LooseChapter],
        timesource: TimeSourceT = Fraction(24000, 1001),
        timescale: TimeScaleT = TimeScale.MKV,
        _print: bool = True,
    ) -> None:
        """
        Convenience class for chapters

        :param chapter_source:      Input either txt with ogm chapters, xml or (a list of) self defined chapters.
        :param timesource:          The source of timestamps/timecodes. For details check the docstring on the type.
        :param timescale:           Unit of time (in seconds) in terms of which frame timestamps are represented.\n
                                    For details check the docstring on the type.
        :param _print:              Prints chapters after parsing and after trimming.
        """
        self.timestamps = resolve_timesource_and_scale(timesource, timescale, caller=self)
        if isinstance(chapter_source, tuple):
            chapters = [chapter_source]
        elif isinstance(chapter_source, list):
            chapters = chapter_source
        else:
            chapter_source = ensure_path(chapter_source, self)
            chapters = parse_xml(chapter_source) if chapter_source.suffix.lower() == ".xml" else parse_ogm(chapter_source)  # type: ignore

        # Convert all framenumbers to timedeltas
        normalized_chapters = list[Chapter]()
        for time_value, name in chapters:
            if isinstance(time_value, int):
                ms = self.timestamps.frame_to_time(time_value, TimeType.START, 3)
                normalized_chapters.append((timedelta(milliseconds=ms), name))
            else:
                normalized_chapters.append((time_value, name))
        self.chapters = normalized_chapters

        if _print and isinstance(chapter_source, Path):
            self.print()

    def _frame(self, time: timedelta) -> int:
        return timedelta_to_frame(self.timestamps, time, TimeType.START)

    def trim(self: ChaptersSelf, trim_start: int = 0, trim_end: int | None = 0, num_frames: int = 0) -> ChaptersSelf:
        """
        Trims the chapters the same way the video and audio are trimmed, keeping the frames from `trim_start` up to `trim_end`.\n
        Both are frame numbers of the untrimmed source, like `clip[trim_start:trim_end]` in VapourSynth.

        The chapter that is playing at `trim_start` is moved to the start, every chapter before it is dropped.

        :param trim_start:  First frame to keep.
        :param trim_end:    Frame to stop before. `0` or `None` keeps everything after `trim_start`.\n
                            Negative values count back from the end of the source.
                            Because the length of the source isn't known here, this requires `num_frames`.
        :param num_frames:  Number of frames of the trimmed clip. Chapters that would start at or after its end are dropped.
        """
        trim_start = max(trim_start, 0)
        trim_end = trim_end or None
        if trim_end is not None and trim_end < 0:
            if not num_frames:
                raise error("A negative trim_end requires num_frames to be passed!", self)
            # The end of the trimmed clip is already given by num_frames
            trim_end = None

        start_ms = self.timestamps.frame_to_time(trim_start, TimeType.START, 3)
        chapters: list[Chapter] = []
        for time_value, name in sorted(self.chapters, key=lambda chapter: chapter[0]):
            frame = self._frame(time_value)
            if trim_end is not None and frame >= trim_end:
                continue

            new_frame = max(frame - trim_start, 0)
            if num_frames and new_frame >= num_frames:
                continue

            # Only the last chapter at or before trim_start stays, as it is the one playing when the trimmed clip starts
            if new_frame == 0 and chapters and chapters[-1][0] == timedelta(0):
                chapters.pop()

            time_value = max(time_value - timedelta(milliseconds=start_ms), timedelta(0))
            # Subtracting two millisecond values that were rounded on their own can land on the next frame
            if isinstance(self.timestamps, FPSTimestamps) and self._frame(time_value) != new_frame:
                time_value = timedelta(milliseconds=self.timestamps.frame_to_time(new_frame, TimeType.START, 3))
            chapters.append((time_value, name))

        self.chapters = chapters
        return self

    def set_names(self: ChaptersSelf, names: list[str | None]) -> ChaptersSelf:
        """
        Renames the chapters

        :param names:   List of names
        """
        old: list[str | None] = [chapter[1] for chapter in self.chapters]
        if len(names) > len(old):
            self.print()
            raise error("Chapters: too many names!", self)
        if len(names) < len(old):
            names += [None] * (len(old) - len(names))

        chapters: list[Chapter] = []
        for i, name in enumerate(names):
            time, _ = self.chapters[i]
            chapters.append((time, name))

        self.chapters = chapters
        return self

    def add(self: ChaptersSelf, chapters: LooseChapter | list[LooseChapter] | list[Chapter], index: int = 0) -> ChaptersSelf:
        """
        Adds a chapter at the specified index
        """
        if isinstance(chapters, tuple):
            chapters = [chapters]
        else:
            chapters = chapters

        converted: list[Chapter] = []
        for ch in chapters:
            time_value, name = ch
            if isinstance(time_value, int):
                ms = self.timestamps.frame_to_time(time_value, TimeType.START, 3)
                converted.append((timedelta(milliseconds=ms), name))
            else:
                converted.append(cast(Chapter, ch))

        for ch in converted:
            self.chapters.insert(index, ch)
            index += 1
        return self

    def shift_chapter(self: ChaptersSelf, chapter: int = 0, shift_amount: int = 0) -> ChaptersSelf:
        """
        Used to shift a single chapter by x frames

        :param chapter:         Chapter number (starting at 0)
        :param shift_amount:    Frames to shift by
        """
        time, name = self.chapters[chapter]
        ch_frame = timedelta_to_frame(self.timestamps, time, TimeType.START) + shift_amount
        if ch_frame > 0:
            ms = self.timestamps.frame_to_time(ch_frame, TimeType.START, 3)
            time = timedelta(milliseconds=ms)
        else:
            time = timedelta(seconds=0)
        self.chapters[chapter] = (time, name)
        return self

    def shift(self: ChaptersSelf, shift_amount: int) -> ChaptersSelf:
        """
        Shifts all chapters by x frames.

        :param shift_amount:    Frames to shift by
        """
        return [self.shift_chapter(i, shift_amount) for i, _ in enumerate(self.chapters)][-1]

    def print(self: ChaptersSelf) -> ChaptersSelf:
        """
        Prettier print for these because default timedelta formatting sucks
        """
        info("Chapters:")
        for time, name in self.chapters:
            frame = timedelta_to_frame(self.timestamps, time, TimeType.START)
            print(f"{name}: {format_timedelta(time)} | {frame}")
        print("", end="\n")
        return self

    def to_file(self: ChaptersSelf, out: PathLike | None = None, minus_one_ms_hack: bool = True) -> Path:
        """
        Outputs the chapters to an OGM file

        :param out:                 Can be either a directory or a full file path
        :param minus_one_ms_hack:   If True, every chapter will be shifted by -1ms to avoid issues with some players
        """
        if not out:
            out = get_workdir()
        out = ensure_path(out, self)
        if out.is_dir():
            out_file = out / "chapters.txt"
        else:
            out_file = out
        with open(out_file, "w", encoding="utf_8_sig") as f:
            chapters = [
                "CHAPTER{num:02d}={time}\nCHAPTER{num:02d}NAME={name}\n".format(
                    num=i + 1,
                    time=format_timedelta(
                        (chapter[0] - timedelta(milliseconds=1)) if minus_one_ms_hack and chapter[0] > timedelta(milliseconds=0) else chapter[0]
                    ),
                    name=chapter[1],
                )
                for i, chapter in enumerate(sorted(self.chapters, key=lambda x: x[0]))
            ]
            f.writelines(chapters)
        return out_file

    @staticmethod
    def from_sub(
        file: PathLike | SubFile,
        timesource: TimeSourceT = None,
        timescale: TimeScaleT = None,
        use_actor_field: bool = False,
        markers: str | list[str] = ["chapter", "chptr"],
        _print: bool = True,
        encoding: str = "utf_8_sig",
    ) -> "Chapters":
        """
        Extract chapters from an ass file or a SubFile.

        :param file:            Input ass file or SubFile
        :param timesource:      The source of timestamps/timecodes. For details check the docstring on the type.
        :param timescale:       Unit of time (in seconds) in terms of which frame timestamps are represented.\n
                                For details check the docstring on the type.
        :param use_actor_field: Uses the actor field instead of the effect field for identification.
        :param markers:         Markers to check for.
        :param _print:          Prints the chapters after parsing
        :param encoding:        Encoding used to read the ass file if need be
        """
        from ass import parse_file, Comment  # type: ignore[import-untyped]

        caller = "Chapters.from_sub"

        if isinstance(markers, str):
            markers = [markers]

        if isinstance(file, SubFile):
            doc = file._read_doc()
        else:
            file = ensure_path_exists(file, caller)
            with open(file if not file else file, "r", encoding=encoding) as reader:
                doc = parse_file(reader)

        pattern = re.compile(r"\{([^\\=].+?)\}")
        chapters = list[Chapter]()
        for line in doc.events:
            field_value = str(line.name).lower() if use_actor_field else str(line.effect).lower()
            found = [m in field_value for m in markers]
            if any(found):
                match = pattern.search(line.text)
                if match:
                    chapters.append((line.start, match.group(1)))
                elif isinstance(line, Comment) and line.text:
                    chapters.append((cast(_Line, line).start, str(line.text).strip()))
                else:
                    warn(f"Chapter {(len(chapters) + 1):02.0f} does not have a name!", caller)
                    chapters.append((line.start, ""))

        if not chapters:
            warn("Could not find any chapters in subtitle!", caller)

        if timesource is None and (setup_timesource := get_setup_attr("sub_timesource", None)) is not None:
            if not isinstance(setup_timesource, TimeSourceT):
                raise error("Invalid timesource type in Setup!", caller)
            debug("Using default timesource from setup.", caller)
            timesource = setup_timesource

        if timescale is None and (setup_timescale := get_setup_attr("sub_timescale", None)) is not None:
            if not isinstance(setup_timescale, TimeScaleT):
                raise error("Invalid timescale type in Setup!", caller)
            debug("Using default timescale from setup.", caller)
            timescale = setup_timescale

        ch = Chapters(chapters, timesource, timescale)  # type: ignore
        if _print and chapters:
            ch.print()
        return ch

    @staticmethod
    def from_mkv(file: PathLike, timesource: TimeSourceT = None, timescale: TimeScaleT = None, _print: bool = True, quiet: bool = True) -> "Chapters":
        """
        Extract chapters from mkv.

        :param file:            Input mkv file
        :param timesource:      The source of timestamps/timecodes. For details check the docstring on the type.
        :param timescale:       Unit of time (in seconds) in terms of which frame timestamps are represented.\n
                                For details check the docstring on the type.
        :param _print:          Prints the chapters after parsing
        """
        caller = "Chapters.from_mkv"
        file = ensure_path_exists(file, caller)

        mkvextract = get_executable("mkvextract")
        out = Path(get_temp_workdir(), f"{file.stem}_chapters.txt")
        args = [mkvextract, str(file), "chapters", "-s", str(out)]
        if run_commandline(args, quiet):
            raise error("Failed to extract chapters!", caller)

        if timesource is None:
            chapters = Chapters(out, file, _print=_print)
        else:
            chapters = Chapters(out, timesource, timescale, _print)

        clean_temp_files()
        return chapters


ChaptersSelf = TypeVar("ChaptersSelf", bound=Chapters)
