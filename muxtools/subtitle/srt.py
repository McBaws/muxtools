import re
from typing import NamedTuple

__all__: list[str] = []

SRT_TAG_REGEX = re.compile(r"<(/?)([a-zA-Z]+)(\s[^<>]*)?/?>")
FONT_COLOR_REGEX = re.compile(r"""\bcolor\s*=\s*["']?#?([0-9a-fA-F]{6})(?![0-9a-fA-F])""", re.I)

# Lenient like ffmpeg (libavformat/srtdec.c) about spacing, digit counts and the separator, and anything after the end time is ignored
_TIMESTAMP = r"\s*(\d+):\s*(\d+):\s*(\d+)[,.]\s*(\d+)"
SRT_TIMING_REGEX = re.compile(rf"{_TIMESTAMP}\s*-->{_TIMESTAMP}")


class SRTEvent(NamedTuple):
    start: int
    """Start in milliseconds."""
    end: int
    """End in milliseconds."""
    text: str
    """Text converted to ASS."""


def _srt_tags_to_ass(text: str, font_tags: bool = False) -> str:
    """
    Converts <b>, <i>, <s>, <u> and <br> in any casing to ASS tags.
    <font> tags are removed, and their color is converted if `font_tags` is set.
    Anything else is kept as text, since it's more likely to be text like "<Korean dialog>" than a tag.
    """
    # Whether each open <font> tag set a color, so its closing tag can reset it
    open_fonts = list[bool]()

    def replace(match: re.Match[str]) -> str:
        tag_close = match[1] == "/"
        name = match[2].lower()

        if name in ("b", "i", "s", "u"):
            return f"{{\\{name}{'' if tag_close else 1}}}"

        if name == "br":
            return "\\N"

        if name != "font":
            return match[0]

        if tag_close:
            return R"{\c}" if open_fonts and open_fonts.pop() else ""

        color = ""
        if font_tags and (rgb := FONT_COLOR_REGEX.search(match[3] or "")):
            color = f"{{\\c&H{(rgb[1][4:6] + rgb[1][2:4] + rgb[1][0:2]).upper()}&}}"
        open_fonts.append(bool(color))
        return color

    return SRT_TAG_REGEX.sub(replace, text)


def _srt_text_to_ass(text: str, font_tags: bool) -> str:
    """
    Converts the text of an event to ASS, including the old {i} style tags.
    Spaces and tabs around every line and lines that end up empty are removed.
    """
    text = re.sub(r"\{(/?)([bisu])\}", lambda m: f"{{\\{m[2]}{'' if m[1] else 1}}}", text)
    lines = (line.strip(" \t") for line in _srt_tags_to_ass(text, font_tags).split("\n"))
    return "\\N".join(line for line in lines if line)


def parse_srt(content: str, font_tags: bool = False) -> list[SRTEvent]:
    """
    Parses SRT mostly like ffmpeg does (srt_read_header in libavformat/srtdec.c).

    An event starts at a timing line and its text goes until the next timing line, so blank lines inside the text don't end it.
    A line with just a number right before a timing line is taken as the event index and dropped.
    The events are sorted by start time, an event ending before its start lasts until the next one and exact duplicates are dropped.

    :param content:     Content of the SRT file
    :param font_tags:   Convert the color of <font> tags instead of only removing them
    """
    # (start, end, text) for every event
    raw: list[tuple[int, int, str]] = []
    lines = list[str]()
    # A number might be the index of the next event, which is only known at the next line
    cache = ""
    timing: re.Match[str] | None = None

    def add_event() -> None:
        assert timing is not None
        if lines:
            values = [int(group) for group in timing.groups()]
            start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
            end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
            raw.append((start, end, "\n".join(lines)))
            lines.clear()

    for line in re.split(r"\r\n?|\n", content):
        if not line:
            continue

        if match := SRT_TIMING_REGEX.match(line):
            if timing is not None:
                add_event()
            cache = ""
            timing = match
        elif timing is not None:
            # The cached number was followed by more text, so it's part of the text after all
            if cache:
                lines.append(cache)
                cache = ""
            if re.fullmatch(r"\s*\d+\s*", line):
                cache = line
            else:
                lines.append(line)

    # A trailing number is more likely to be text (like a copyright year) than the index of a missing event
    if timing is not None:
        if cache:
            lines.append(cache)
        add_event()

    raw.sort(key=lambda event: event[0])
    for i in range(len(raw) - 1):
        start, end, text = raw[i]
        if end < start:
            raw[i] = (start, raw[i + 1][0], text)

    return [
        SRTEvent(start, end, _srt_text_to_ass(text, font_tags))
        for i, (start, end, text) in enumerate(raw)
        if not i or raw[i - 1] != (start, end, text)
    ]
