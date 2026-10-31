from muxtools import SubFile, FontFile, Setup, get_workdir, collect_fonts, subset_fonts, LoggingException
from muxtools.subtitle.subutils import create_document
import muxtools.subtitle.font as font_module
from ass import Dialogue, Style
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.ttCollection import TTCollection
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from shutil import move, rmtree
import logging
from time import sleep
import pytest


@pytest.fixture(autouse=True)
def setup_and_remove():
    Setup("Test", None)

    yield

    sleep(0.1)
    rmtree(get_workdir())


def _build_font(family: str, bold: bool = False, chars: str = "abcdefghijklmnopqrstuvwxyz ", cff: bool = False) -> TTFont:
    """
    Builds a minimal font with a square glyph for every character in `chars`.
    It's a TrueType font, or an OpenType one with CFF outlines if `cff` is set.
    """
    glyph_names = {ord(c): f"uni{ord(c):04X}" for c in chars}
    glyph_order = [".notdef", *glyph_names.values()]
    full_name = f"{family} Bold" if bold else family
    ps_name = f"{family.replace(' ', '')}-{'Bold' if bold else 'Regular'}"

    def square(pen):
        pen.moveTo((0, 0))
        pen.lineTo((0, 500))
        pen.lineTo((500, 500))
        pen.lineTo((500, 0))
        pen.closePath()
        return pen

    builder = FontBuilder(1000, isTTF=not cff)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(glyph_names)
    if cff:
        builder.setupCFF(ps_name, {"FullName": full_name}, {name: square(T2CharStringPen(600, None)).getCharString() for name in glyph_order}, {})
    else:
        builder.setupGlyf({name: square(TTGlyphPen(None)).glyph() for name in glyph_order})
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": family, "styleName": "Bold" if bold else "Regular", "fullName": full_name, "psName": ps_name})
    builder.setupOS2(usWeightClass=700 if bold else 400, fsSelection=0x20 if bold else 0x40)
    builder.setupPost()
    builder.updateHead(macStyle=1 if bold else 0)
    return builder.font


def _make_font(path: Path, family: str, bold: bool = False) -> Path:
    _build_font(family, bold, cff=path.suffix == ".otf").save(path)
    return path


def _make_collection(path: Path, family: str) -> Path:
    """
    Builds a collection with a Regular and a Bold face of the family.
    """
    collection = TTCollection()
    collection.fonts = [_build_font(family), _build_font(family, bold=True)]
    collection.save(path)
    return path


def _write_sub(path: Path, styles: list[tuple[str, str]], lines: list[tuple[str, str]]) -> SubFile:
    """
    :param styles:  (style name, font name) pairs
    :param lines:   (style name, text) pairs
    """
    doc = create_document()
    doc.styles.extend(Style(name=name, fontname=font) for name, font in styles)
    doc.events.extend(Dialogue(style=style, text=text) for style, text in lines)
    with open(path, "w", encoding="utf_8_sig") as writer:
        doc.dump_file(writer)
    return SubFile(path)


class _RecordWarnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages = list[str]()

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _warnings() -> Iterator[list[str]]:
    """
    Collects everything muxtools logs as a warning or worse.
    """
    handler = _RecordWarnings()
    logger = logging.getLogger("muxtools")
    logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)


def test_collect_fonts_multiple_subs(tmp_path: Path) -> None:
    """
    Collecting for several subs at once gets the fonts of all of them.
    Different fonts that end up with the same file name in the workdir are both kept, even when used by different subs.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "shared.ttf", "MT Test")
    _make_font(source / "one.ttf", "Foo Bar")
    _make_font(source / "two.ttf", "Foo/Bar")
    first = _write_sub(tmp_path / "first.ass", [("Default", "MT Test"), ("Sign", "Foo Bar")], [("Default", "abc"), ("Sign", "abc")])
    second = _write_sub(tmp_path / "second.ass", [("Default", "MT Test"), ("Sign", "Foo/Bar")], [("Default", "def"), ("Sign", "def")])

    Setup("Test", None, error_on_danger=True)
    with _warnings() as warnings:
        collected = collect_fonts([first, second], use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    assert not warnings
    assert len(collected) == 3


def test_collect_fonts_separate_calls(tmp_path: Path) -> None:
    """
    Collecting again reuses the files already in the workdir, but not for a different font that ends up with the same file name.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "one.ttf", "Foo Bar")
    _make_font(source / "two.ttf", "Foo/Bar")
    first = _write_sub(tmp_path / "first.ass", [("Default", "Foo Bar")], [("Default", "abc")])
    second = _write_sub(tmp_path / "second.ass", [("Default", "Foo/Bar")], [("Default", "def")])

    collect_fonts(first, use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    collect_fonts(second, use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    collected = collect_fonts(first, use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    assert len(collected) == 2


def test_collect_fonts_checks_repeated_face(tmp_path: Path) -> None:
    """
    Missing glyphs are reported even when an earlier style already used the same font.
    """
    # There's no bold face so the bold text resolves to the regular one again
    _make_font(tmp_path / "font.ttf", "MT Test")
    sub = _write_sub(tmp_path / "sub.ass", [("Default", "MT Test")], [("Default", "abc"), ("Default", R"{\b1}abc€")])

    Setup("Test", None, error_on_danger=True)
    with pytest.raises(LoggingException, match="missing the following glyphs"):
        sub.collect_fonts(use_system_fonts=False, search_current_dir=False, additional_fonts=[tmp_path])


@pytest.mark.parametrize("ignore_fonts_with_no_usage", [True, False])
def test_subset_round_trip(tmp_path: Path, ignore_fonts_with_no_usage: bool) -> None:
    """
    Subset fonts have to be found again exactly like the original ones.

    The fonts are collected, subset and then collected again from only the subset files.
    Both collections have to find every font without any warnings, like faux bold because a face is missing from its family.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    # A family split over two files where each face uses different characters
    _make_font(source / "regular.ttf", "MT Test")
    _make_font(source / "bold.ttf", "MT Test", bold=True)
    # A collection where the bold face is only used for a drawing, so it has no characters to subset
    _make_collection(source / "pair.ttc", "MT Pair")
    # A family where only the bold face is skipped, like a font under the minimum size would be
    _make_font(source / "skip_regular.ttf", "MT Skip")
    _make_font(source / "skip_bold.ttf", "MT Skip", bold=True)

    sub = _write_sub(
        tmp_path / "sub.ass",
        [("Default", "MT Test"), ("Pair", "MT Pair"), ("Skip", "MT Skip")],
        [
            ("Default", "abc"),
            ("Default", R"{\b1}xyz"),
            ("Pair", "ghi"),
            ("Pair", R"{\b1\p1}m 0 0 l 100 0 100 100 0 100{\p0}"),
            ("Skip", "jkl"),
            ("Skip", R"{\b1}mno"),
        ],
    )
    Setup("Test", None, error_on_danger=True)

    with _warnings() as warnings:
        collected = collect_fonts(sub, use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    assert not warnings
    assert len(collected) == 5

    # Aggressive so every face only keeps the characters it uses, which makes the drawing-only face fall back
    # to the common subset when it isn't skipped for having no usage
    subset = subset_fonts(
        [sub],
        aggressive=True,
        ignore_fonts_with_no_usage=ignore_fonts_with_no_usage,
        min_file_size_to_subset=0,
        skip_fonts=["MTSkip-Bold"],
        print_final_stats=False,
    )
    assert len(subset) == 5

    subset_dir = tmp_path / "subset"
    subset_dir.mkdir()
    for font in subset:
        move(font.file, subset_dir)

    with _warnings() as warnings:
        collected = collect_fonts(sub, use_system_fonts=False, search_current_dir=False, additional_fonts=subset_dir)
    assert not warnings
    assert len(collected) == 5


def test_subset_renames_font_tags(tmp_path: Path) -> None:
    """
    Font names are renamed to the names of the subset fonts no matter how they're cased, as renderers ignore the case.
    Tags using the same font several times in a line all get renamed once.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "regular.ttf", "MT Test")
    _make_font(source / "bold.ttf", "MT Test", bold=True)

    sub = _write_sub(
        tmp_path / "sub.ass",
        [("Default", "mt test")],
        [
            ("Default", R"{\fnMT Test}abc{\fnMT Test}def"),
            ("Default", R"{\fnmt test\b1}ghi"),
            ("Default", R"{\fnmt test bold\b1}jkl"),
        ],
    )
    Setup("Test", None, error_on_danger=True)

    collect_fonts(sub, use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    subset = subset_fonts([sub], min_file_size_to_subset=0, print_final_stats=False)

    regular, bold = _by_weight(subset)
    family = regular["name"].getDebugName(1)
    full_bold = bold["name"].getDebugName(4)
    assert bold["name"].getDebugName(1) == family

    doc = sub._read_doc()
    assert doc.styles[0].fontname == family
    assert [line.text for line in doc.events] == [
        Rf"{{\fn{family}}}abc{{\fn{family}}}def",
        Rf"{{\fn{family}\b1}}ghi",
        Rf"{{\fn{full_bold}\b1}}jkl",
    ]

    subset_dir = tmp_path / "subset"
    subset_dir.mkdir()
    for font in subset:
        move(font.file, subset_dir)

    with _warnings() as warnings:
        collect_fonts(sub, use_system_fonts=False, search_current_dir=False, additional_fonts=subset_dir)
    assert not warnings


def _collect_from_subset(tmp_path: Path, subs: list[SubFile], fonts: list[FontFile]) -> list[str]:
    """
    Moves the subset fonts out of the workdir and collects the subs again from only those.

    :return:    The warnings that came up
    """
    subset_dir = tmp_path / "subset"
    subset_dir.mkdir()
    for font in fonts:
        move(font.file, subset_dir)

    with _warnings() as warnings:
        collect_fonts(subs, use_system_fonts=False, search_current_dir=False, additional_fonts=subset_dir)
    return warnings


def _subset(subs: list[SubFile], source: Path, **kwargs) -> list[FontFile]:
    Setup("Test", None, error_on_danger=True)
    collect_fonts(subs, use_system_fonts=False, search_current_dir=False, additional_fonts=source)
    return subset_fonts(subs, min_file_size_to_subset=0, print_final_stats=False, **kwargs)


def _by_weight(fonts: list[FontFile]) -> list[TTFont]:
    return sorted((TTFont(font.file) for font in fonts), key=lambda font: font["OS/2"].usWeightClass)  # type: ignore[attr-defined]


def test_subset_multiple_subs(tmp_path: Path) -> None:
    """
    Subs subset together share one subset font with the characters of all of them.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "font.ttf", "MT Test")
    first = _write_sub(tmp_path / "first.ass", [("Default", "MT Test")], [("Default", "abc")])
    second = _write_sub(tmp_path / "second.ass", [("Default", "mt test")], [("Default", "xyz")])

    subset = _subset([first, second], source, aggressive=True)
    assert len(subset) == 1

    font = TTFont(subset[0].file)
    characters = {chr(code) for code in font.getBestCmap() or {}}
    assert set("abcxyz") <= characters
    assert "d" not in characters

    family = font["name"].getDebugName(1)
    assert first._read_doc().styles[0].fontname == second._read_doc().styles[0].fontname == family
    assert not _collect_from_subset(tmp_path, [first, second], subset)


def test_subset_vertical_font_name(tmp_path: Path) -> None:
    """
    The @ in front of font names for vertical text is kept in front of the new name.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "font.ttf", "MT Test")
    sub = _write_sub(tmp_path / "sub.ass", [("Default", "@MT Test")], [("Default", "abc"), ("Default", R"{\fn@mt test}def")])

    subset = _subset([sub], source)
    family = TTFont(subset[0].file)["name"].getDebugName(1)

    doc = sub._read_doc()
    assert doc.styles[0].fontname == f"@{family}"
    assert doc.events[1].text == Rf"{{\fn@{family}}}def"
    assert not _collect_from_subset(tmp_path, [sub], subset)


def test_subset_full_name_keeps_face(tmp_path: Path) -> None:
    """
    A full name keeps pointing to its face, even when the line doesn't ask for its weight.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "regular.ttf", "MT Test")
    _make_font(source / "bold.ttf", "MT Test", bold=True)
    sub = _write_sub(tmp_path / "sub.ass", [("Default", "MT Test")], [("Default", "abc"), ("Default", R"{\fnMT Test Bold}def")])

    _, bold = _by_weight(_subset([sub], source))
    assert sub._read_doc().events[1].text == Rf"{{\fn{bold['name'].getDebugName(4)}}}def"


def test_subset_opentype_fonts(tmp_path: Path) -> None:
    """
    OpenType fonts are matched by their PostScript name instead of the full name, so that has to be renamed too.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "regular.otf", "MT Open")
    _make_font(source / "bold.otf", "MT Open", bold=True)
    sub = _write_sub(tmp_path / "sub.ass", [("Default", "MT Open")], [("Default", "abc"), ("Default", R"{\fnMTOpen-Bold\b1}def")])

    subset = _subset([sub], source)
    regular, bold = _by_weight(subset)

    doc = sub._read_doc()
    assert doc.styles[0].fontname == regular["name"].getDebugName(1)
    assert doc.events[1].text == Rf"{{\fn{bold['name'].getDebugName(6)}\b1}}def"
    assert not _collect_from_subset(tmp_path, [sub], subset)


def test_subset_twice(tmp_path: Path) -> None:
    """
    Running it again leaves the fonts that were already subset alone.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "regular.ttf", "MT Test")
    _make_font(source / "bold.ttf", "MT Test", bold=True)
    sub = _write_sub(tmp_path / "sub.ass", [("Default", "MT Test")], [("Default", "abc"), ("Default", R"{\b1}def")])

    first = _subset([sub], source)
    names = {font.file.name: TTFont(font.file)["name"].getDebugName(1) for font in first}
    text = sub.file.read_text("utf_8_sig")

    second = subset_fonts([sub], min_file_size_to_subset=0, print_final_stats=False)
    assert {font.file.name: TTFont(font.file)["name"].getDebugName(1) for font in second} == names
    assert sub.file.read_text("utf_8_sig") == text


def test_subset_checks_renamed_subs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    It errors when a sub would end up using a different font after renaming, instead of silently breaking it.
    """
    source = tmp_path / "fonts"
    source.mkdir()
    _make_font(source / "font.ttf", "MT Test")
    sub = _write_sub(tmp_path / "sub.ass", [("Default", "MT Test")], [("Default", R"{\fnMT Test}abc")])

    # Simulate a bug where the font tags don't get renamed
    monkeypatch.setattr(font_module, "_replace_font_tags", lambda text, replacements: text)
    with pytest.raises(LoggingException, match="Subsetting changed which fonts are used"):
        _subset([sub], source)
