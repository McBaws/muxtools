import re
from typing import NamedTuple

__all__: list[str] = []

SRT_TAG_REGEX = re.compile(r"<(/?)([a-zA-Z]+)(\s[^<>]*)?/?>")
FONT_ATTRIBUTE_REGEX = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")

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


def _srt_tags_to_ass(text: str, play_res_y: int = 1080, font_tags: bool = False) -> str:
    """
    Converts <b>, <i>, <s>, <u> and <br> in any casing to ASS tags.
    <font> tags with color, face and size are converted too if `font_tags` is set and removed otherwise.
    Anything else is kept as text, since it's more likely to be text like "<Korean dialog>" than a tag.

    :param play_res_y:  Height the font sizes are scaled to
    """
    # What the last <font> tag set, so its closing tag can reset it
    font_resets = list[str]()

    def replace(match: re.Match[str]) -> str:
        tag_close = match[1] == "/"
        name = match[2].lower()

        if name in ("b", "i", "s", "u"):
            return f"{{\\{name}{'' if tag_close else 1}}}"

        if name == "br":
            return "\\N"

        if name != "font":
            return match[0]

        if not font_tags:
            return ""

        if tag_close:
            out = "{" + "".join(f"\\{tag}" for tag in font_resets) + "}" if font_resets else ""
            font_resets.clear()
            return out

        tags = dict[str, str]()
        for attribute in FONT_ATTRIBUTE_REGEX.finditer(match[3] or ""):
            key, value = attribute[1].lower(), next(v for v in attribute.groups()[1:] if v is not None)
            if key == "color" and (rgb := re.fullmatch(r"#?([0-9a-fA-F]{6})", value)):
                tags["c"] = f"&H{(rgb[1][4:6] + rgb[1][2:4] + rgb[1][0:2]).upper()}&"
            elif key == "face" and value:
                tags["fn"] = value
            elif key == "size" and value.isdigit():
                # ffmpeg writes the size as is into a 288 high script, which is how players based on it show these
                tags["fs"] = str(round(int(value) * play_res_y / 288))
        font_resets[:] = tags
        return "{" + "".join(f"\\{tag}{value}" for tag, value in tags.items()) + "}" if tags else ""

    return SRT_TAG_REGEX.sub(replace, text)


def _srt_text_to_ass(text: str, play_res_y: int, font_tags: bool) -> str:
    """
    Converts the text of an event to ASS, including the old {i} style tags.
    Spaces and tabs around every line and lines that end up empty are removed.
    """
    text = re.sub(r"\{(/?)([bisu])\}", lambda m: f"{{\\{m[2]}{'' if m[1] else 1}}}", text)
    lines = (line.strip(" \t") for line in _srt_tags_to_ass(text, play_res_y, font_tags).split("\n"))
    return "\\N".join(line for line in lines if line)


def parse_srt(content: str, play_res_y: int = 1080, font_tags: bool = False) -> list[SRTEvent]:
    """
    Parses SRT mostly like ffmpeg does (srt_read_header in libavformat/srtdec.c).

    An event starts at a timing line and its text goes until the next timing line, so blank lines inside the text don't end it.
    A line with just a number right before a timing line is taken as the event index and dropped.
    The events are sorted by start time, an event ending before its start lasts until the next one and exact duplicates are dropped.

    :param content:     Content of the SRT file
    :param play_res_y:  Height the font sizes are scaled to
    :param font_tags:   Convert <font> tags instead of removing them
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

    for line in re.split(r"\r\n?|\n", content.lstrip("﻿")):
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
        SRTEvent(start, end, _srt_text_to_ass(text, play_res_y, font_tags))
        for i, (start, end, text) in enumerate(raw)
        if not i or raw[i - 1] != (start, end, text)
    ]
