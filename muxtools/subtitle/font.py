from __future__ import annotations
import os
import re
import shutil
import filecmp
import logging
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import base64

if TYPE_CHECKING:
    from font_collector import ABCFontFace, AssStyle, FontResult
    from fontTools.ttLib import TTFont  # type: ignore[import-untyped]

from .sub import SubFile, FontFile as MTFontFile
from ..utils.convert import sizeof_fmt
from ..utils.env import get_workdir
from ..utils.files import ensure_path_exists
from ..utils.types import PathLike
from ..utils.log import warn, error, info, danger, debug


__all__ = [
    "collect_fonts",
    "subset_fonts",
]


# A selection of common unicode characters to always include when subsetting fonts
# Follows the format: 'U+XXXX'
# For ranges, use 'U+XXXX-YYYY'
UNFORMATTED_COMMON_UNICODE_CHARS = [
    "U+0000-00FF",  # Basic Latin + Latin-1 Supplement
    "U+0100-024F",  # Latin Extended-A + Latin Extended-B
    "U+1E00-1EFF",  # Latin Extended Additional
    "U+2000-206F",  # General Punctuation
    "U+20A0-20CF",  # Currency Symbols
]


def _parse_unicode_chars(char_list: list[str]) -> list[str]:
    """Parse unicode character definitions including ranges."""
    result = []
    for item in char_list:
        if item.startswith("U+"):
            if "-" in item:
                # Handle range
                parts = item.replace("U+", "").split("-")
                start = int(parts[0], 16)
                end = int(parts[1], 16)
                result.extend([chr(i) for i in range(start, end + 1)])
            else:
                # Single character
                result.append(chr(int(item.replace("U+", ""), 16)))
        else:
            if len(item) == 1:
                result.append(item)
            else:
                warn(f"Invalid unicode character format: {item}", _parse_unicode_chars)

    return result


COMMON_UNICODE_CHARS = _parse_unicode_chars(UNFORMATTED_COMMON_UNICODE_CHARS)

# Put in name ID 3 (unique font identifier) of every font subset_fonts wrote, so they aren't renamed again
SUBSET_MARKER = "muxtools-subset;"


def _subset_name(name: str, key: str) -> str:
    """
    Builds the new name for a font name, unique to the fonts and characters in `key`.

    The hash goes first because GDI doesn't distinguish names after 31 characters.
    The rest is kept PostScript-safe, so the same name works as a family, full and PostScript name.
    """
    digest = base64.urlsafe_b64encode(hashlib.sha256(f"{key}_{name.lower()}".encode("utf-8")).digest()).decode("utf-8").replace("=", "")
    readable = re.sub(r"[^\x21-\x7e]|[\[\](){}<>/%]", "", name)
    # PostScript names can be at most 63 characters long
    return f"{digest[:16]}_{readable}"[:63]


def _replace_font_name(name: str, replacements: dict[str, str]) -> str:
    # Renderers ignore the case and surrounding whitespace, and a leading @ only asks for vertical text
    stripped = name.strip()
    vertical = stripped.startswith("@")
    replacement = replacements.get(stripped.removeprefix("@").lower())
    if replacement is None:
        return name
    return f"@{replacement}" if vertical else replacement


def _replace_font_tags(text: str, replacements: dict[str, str]) -> str:
    # Every \fn tag is replaced in a single pass, so a name that was already replaced can't be matched again
    def replace_block(block: re.Match[str]) -> str:
        return re.sub(r"(\\fn)([^\\}]*)", lambda tag: tag.group(1) + _replace_font_name(tag.group(2), replacements), block.group(0))

    return re.sub(r"\{[^}]*\}", replace_block, text)


def _is_already_subset(font_face: ABCFontFace) -> bool:
    from fontTools.ttLib import TTFont  # type: ignore[import-untyped]

    assert font_face.font_file is not None, "Font file is missing!"
    with TTFont(font_face.font_file.filename, fontNumber=font_face.font_index, lazy=True) as font:
        return any(record.nameID == 3 and record.toUnicode(errors="replace").startswith(SUBSET_MARKER) for record in font["name"].names)


def _group_families(faces: list[ABCFontFace]) -> list[list[ABCFontFace]]:
    """
    Groups the faces into families the way renderers see them, so faces sharing any family name end up together.
    """
    groups: list[tuple[set[str], list[ABCFontFace]]] = []
    for face in faces:
        names = {name.value.lower() for name in face.family_names}
        members = [face]
        for group in [group for group in groups if group[0] & names]:
            groups.remove(group)
            names |= group[0]
            members = group[1] + members
        groups.append((names, members))
    return [members for _, members in groups]


def _rewrite_names(font: TTFont, family: str, exact: str) -> None:
    """
    Replaces every name of a face with the new family and exact (full and PostScript) name.

    The records are written for every platform the font had names for, as renderers only read the names of the platform their cmap is from.
    Alternative names like the typographic family (ID 16/17) are removed, so nothing refers to the original font anymore.
    """
    table = font["name"]
    subfamily = table.getDebugName(2) or "Regular"
    platforms = {(record.platformID, record.platEncID) for record in table.names if record.nameID in (1, 4, 6)} or {(3, 1)}
    table.names = [record for record in table.names if record.nameID not in (1, 2, 3, 4, 6, 16, 17, 18, 21, 22, 25)]

    for platform_id, encoding_id in sorted(platforms):
        language_id = 0x409 if platform_id == 3 else 0
        for name_id, value in ((1, family), (2, subfamily), (3, f"{SUBSET_MARKER}{exact}"), (4, exact), (6, exact)):
            table.setName(value, name_id, platform_id, encoding_id, language_id)


def subset_fonts(
    subs: list[SubFile],
    aggressive: bool = False,
    ignore_fonts_with_no_usage: bool = True,
    additional_glyphs: list[str] = [],
    min_file_size_to_subset: int = 400 * 1024,
    target_fonts: list[str] | None = None,
    skip_fonts: list[str] | None = None,
    keep_dupe_fonts: bool = False,
    print_final_stats: bool = True,
) -> list[MTFontFile]:
    """
    Subset fonts previously collected with `collect_fonts`. This can greatly reduce the size of the final mux.
    The output of this function should be used instead of the output of `collect_fonts`.
    Pass every sub that gets muxed with the fonts at once, so each font is subset once with the characters all of them use.

    The default behavior is to include the used glyphs and a common set of unicode characters to ensure re-usability of the font for others editing your subtitles.

    Subset fonts get new names that are unique to their glyphs, so they never get mixed up with the full fonts, and the subs are updated to use them.
    Afterwards, every line is checked to still use the same font as before.

    :param subs:                        List of subtitle files to analyze for used glyphs.
    :param aggressive:                  If enabled, this will only include the characters used in the subtitles and all `additional_glyphs` specified.
                                        Note: This may harm the re-usability of the font for others editing your subtitles.
    :param ignore_fonts_with_no_usage:  If no glyphs are used, having this option `True` will skip subsetting for that font.
                                        Otherwise, it will subset using a common glyph set.
    :param additional_glyphs:           If you have any additional glyphs that need to be included in the subsetted fonts.
                                        You can use the format "U+XXXX" for unicode characters or "U+XXXX-YYYY" for unicode ranges.
                                        https://unicode-explorer.com/blocks can help you find the characters/ranges you need.
    :param min_file_size_to_subset:     Only subset fonts whose file size is at least this many bytes. Smaller fonts are left as-is. Set to 0 to subset all fonts regardless of size.
    :param target_fonts:                List of font names to subset. If passed, all other fonts will be left as-is.
    :param skip_fonts:                  List of font names to not subset.
    :param keep_dupe_fonts:             If enabled, any font files that aren't referenced in the provided subs or that were redundantly copied by `collect_fonts` will be kept instead of being deleted.
    :param print_final_stats:           If enabled, will print out statistics about space saved.

    :return:                            A list of FontFile objects
    """

    info("Subsetting fonts...", subset_fonts)

    from font_collector import set_loglevel

    set_loglevel(logging.CRITICAL)

    from fontTools.subset import Subsetter  # type: ignore[import-untyped]
    from fontTools import ttLib  # type: ignore[import-untyped]
    from fontTools.ttLib.ttCollection import TTCollection  # type: ignore[import-untyped]
    from font_collector import AssDocument, FontLoader, FontCollection, FontSelectionStrategyLibass

    strategy = FontSelectionStrategyLibass()
    font_collection = FontCollection(
        use_system_font=False,
        reload_system_font=False,
        use_generated_fonts=False,
        additional_fonts=FontLoader.load_additional_fonts([get_workdir()], scan_subdirs=False),
    )

    # Every font a sub uses with the face it renders with. The renaming and the check afterwards work off of these.
    usages: list[tuple[SubFile, AssStyle, FontResult | None]] = []
    characters: dict[ABCFontFace, set[str]] = defaultdict(set)

    for sub in subs:
        for style, usage_data in AssDocument(sub._read_doc()).get_used_style(collect_draw_fonts=True).items():
            result = font_collection.get_used_font_by_style(style, strategy)
            usages.append((sub, style, result))

            if not result:
                danger(f"Font '{style.fontname}' was not found! Did you run collect_fonts?", subset_fonts)
            else:
                characters[result.font_face].update(usage_data.characters_used)

    faces_by_file: dict[Path, list[ABCFontFace]] = defaultdict(list)
    for font_face in characters:
        assert font_face.font_file is not None, "Font file is missing!"
        faces_by_file[Path(font_face.font_file.filename)].append(font_face)

    # The characters each face is subset to, or None if it's kept whole
    subset_characters: dict[ABCFontFace, set[str] | None] = {}
    additional_characters = set(_parse_unicode_chars(additional_glyphs))

    for source_file, face_list in faces_by_file.items():
        face_list.sort(key=lambda face: face.font_index)

        new_faces = []
        for font_face in face_list:
            if _is_already_subset(font_face):
                debug(f"Font '{_get_fontname(font_face)}' was already subset, keeping it as is.", subset_fonts)
            else:
                new_faces.append(font_face)
        if not new_faces:
            continue

        file_size = os.path.getsize(source_file)
        font_names = {_get_fontname(face) for face in new_faces}

        skip_reason = None
        if skip_fonts is not None and any(name in skip_fonts for name in font_names):
            skip_reason = "in skip_fonts"
        elif target_fonts is not None and not all(name in target_fonts for name in font_names):
            skip_reason = "not in target_fonts"
        elif file_size < min_file_size_to_subset:
            skip_reason = f"{sizeof_fmt(file_size)} < {sizeof_fmt(min_file_size_to_subset)}"

        if skip_reason:
            info(f"Skipping subsetting for '{source_file.name}' ({skip_reason})", subset_fonts)

        for font_face in new_faces:
            face_characters = None
            if not skip_reason:
                face_characters = characters[font_face] | additional_characters
                if not aggressive:
                    face_characters.update(COMMON_UNICODE_CHARS)

                if not face_characters:
                    if ignore_fonts_with_no_usage:
                        warn(f"No characters used in font '{_get_fontname(font_face)}'. Skipping subsetting.", subset_fonts)
                        face_characters = None
                    else:
                        warn(f"No characters used in font '{_get_fontname(font_face)}'. Defaulting to common subset.", subset_fonts)
                        face_characters = set(COMMON_UNICODE_CHARS)

            subset_characters[font_face] = face_characters

    # The new names. Every face of a family is renamed together, including the ones that are kept whole, or they'd stop being one family.
    file_hashes = {source_file: hashlib.sha256(source_file.read_bytes()).hexdigest() for source_file in faces_by_file}
    new_family_names: dict[ABCFontFace, str] = {}
    new_exact_names: dict[ABCFontFace, str] = {}

    for family in _group_families(list(subset_characters)):
        if all(subset_characters[font_face] is None for font_face in family):
            continue

        key = "".join(
            sorted(
                f"{file_hashes[Path(font_face.font_file.filename)]}:{font_face.font_index}:{''.join(sorted(subset_characters[font_face] or []))};"
                for font_face in family
                if font_face.font_file is not None
            )
        )
        family_name = _subset_name((family[0].get_family_name_from_lang("en") or family[0].get_best_family_name()).value, key)
        for font_face in family:
            new_family_names[font_face] = family_name
            new_exact_names[font_face] = _subset_name((font_face.get_exact_name_from_lang("en") or font_face.get_best_exact_name()).value, key)

    # Each font name in the subs gets the new name of the same kind as the one it matched, so it still picks the same face.
    # E.g. a full name like 'Times New Roman Bold' has to keep pointing to the bold face, even without \b1.
    replacements: dict[str, str] = {}
    for _, style, result in usages:
        if result is None or result.font_face not in new_family_names:
            continue

        name = style.fontname.lower()
        is_family_name = any(name == family_name.value.lower() for family_name in result.font_face.family_names)
        new_name = new_family_names[result.font_face] if is_family_name else new_exact_names[result.font_face]

        if replacements.setdefault(name, new_name).lower() != new_name.lower():
            raise error(f"The font name '{style.fontname}' is used for fonts that can't be renamed the same way!", subset_fonts)

    total_old_size = 0
    total_new_size = 0
    output_files: list[Path] = []
    replaced_files: list[Path] = []
    # The face each face in the output files was made from, to check the subs against afterwards
    output_faces: dict[tuple[Path, int], ABCFontFace] = {}

    for source_file, face_list in faces_by_file.items():
        file_size = os.path.getsize(source_file)
        total_old_size += file_size

        if not any(font_face in new_family_names for font_face in face_list):
            # Nothing in this file is subset or renamed, so the original is kept
            total_new_size += file_size
            output_files.append(source_file)
            output_faces.update({(source_file.resolve(), font_face.font_index): font_face for font_face in face_list})
            continue

        fonts: list[TTFont] = []
        for font_face in face_list:
            font = ttLib.TTFont(source_file, fontNumber=font_face.font_index)

            if font_face in new_family_names:
                face_characters = subset_characters[font_face]
                if face_characters is not None:
                    debug(f"Subsetting font '{source_file.name}' (index {font_face.font_index})...", subset_fonts)
                    subsetter = Subsetter()
                    subsetter.populate(text="".join(face_characters))
                    subsetter.subset(font)
                _rewrite_names(font, new_family_names[font_face], new_exact_names[font_face])

            fonts.append(font)

        new_font_path = source_file.with_stem(f"{source_file.stem}_subset")
        if len(fonts) > 1 or source_file.suffix.lower() in (".ttc", ".otc"):
            collection = TTCollection()
            collection.fonts = fonts
            collection.save(str(new_font_path))
        else:
            fonts[0].save(new_font_path)
        for font in fonts:
            font.close()

        output_files.append(new_font_path)
        replaced_files.append(source_file)
        output_faces.update({(new_font_path.resolve(), index): font_face for index, font_face in enumerate(face_list)})

        new_size = os.path.getsize(new_font_path)
        total_new_size += new_size

        for font_face in face_list:
            if font_face not in new_family_names:
                continue
            face_characters = subset_characters[font_face]
            if face_characters is None:
                info(f"Renamed font '{_get_fontname(font_face)}' without subsetting", subset_fonts)
            else:
                info(
                    f"Subsetted font '{_get_fontname(font_face)}' ({len(face_characters)} glyphs, {sizeof_fmt(file_size)} -> {sizeof_fmt(new_size)})",
                    subset_fonts,
                )

    if replacements:
        for sub in subs:
            doc = sub._read_doc()
            modified = False

            for style in doc.styles:
                new_name = _replace_font_name(style.fontname, replacements)
                if new_name != style.fontname:
                    style.fontname = new_name
                    modified = True

            for event in doc.events:
                new_text = _replace_font_tags(event.text, replacements)
                if new_text != event.text:
                    event.text = new_text
                    modified = True

            if modified:
                sub._update_doc(doc)
                info(f"Updated font names in subfile '{sub.file.name}'", subset_fonts)

    _check_subset_usages(subs, usages, replacements, output_files, output_faces)

    for source_file in replaced_files:
        try:
            os.remove(source_file)
        except FileNotFoundError:
            pass
        except PermissionError:
            error(f"Could not remove original font file '{source_file}' due to permission error. Is it open in another program?", subset_fonts)

    known_files = {output_file.resolve() for output_file in output_files}
    found_fonts = [MTFontFile(output_file) for output_file in output_files]

    for r in ["*.[tT][tT][fF]", "*.[oO][tT][fF]", "*.[tT][tT][cC]", "*.[oO][tT][cC]"]:
        for f in get_workdir().glob(r):
            if f.resolve() in known_files:
                continue
            if keep_dupe_fonts:
                debug(f"Keeping duplicate/unreferenced font file '{f.name}'", subset_fonts)
                file_size = os.path.getsize(f)
                total_old_size += file_size
                total_new_size += file_size
                found_fonts.append(MTFontFile(f))
            else:
                # Not used by any of the subs, e.g. a duplicate of another collected file
                debug(f"Removing unreferenced font file '{f.name}'.", subset_fonts)
                try:
                    os.remove(f)
                except OSError:
                    debug(f"Failed to remove unreferenced font file '{f.name}'.", subset_fonts)

    if print_final_stats and total_old_size > 0:
        info(
            f"Subsetting has saved {sizeof_fmt(total_old_size - total_new_size)} ({(total_old_size - total_new_size) / total_old_size * 100:.2f}%, {sizeof_fmt(total_old_size)} -> {sizeof_fmt(total_new_size)})",
            subset_fonts,
        )

    return found_fonts


def _check_subset_usages(
    subs: list[SubFile],
    usages: list[tuple[SubFile, AssStyle, FontResult | None]],
    replacements: dict[str, str],
    output_files: list[Path],
    output_faces: dict[tuple[Path, int], ABCFontFace],
) -> None:
    """
    Makes sure every font a sub uses still renders with the same face, now using only the output fonts.
    """
    from font_collector import AssDocument, AssStyle, FontLoader, FontCollection, FontSelectionStrategyLibass

    strategy = FontSelectionStrategyLibass()
    font_collection = FontCollection(
        use_system_font=False,
        reload_system_font=False,
        use_generated_fonts=False,
        additional_fonts=FontLoader.load_additional_fonts(output_files),
    )

    problems: list[str] = []
    for sub in subs:
        expected = {
            AssStyle(_replace_font_name(style.fontname, replacements), style.weight, style.italic): result
            for usage_sub, style, result in usages
            if usage_sub is sub
        }
        used = AssDocument(sub._read_doc()).get_used_style(collect_draw_fonts=True)

        if set(used) != set(expected):
            problems.append(f"the font names in '{sub.file.name}'")
            continue

        for style, result in expected.items():
            if result is None:
                continue

            new_result = font_collection.get_used_font_by_style(style, strategy)
            source_face = None
            if new_result is not None and new_result.font_face.font_file is not None:
                source_face = output_faces.get((Path(new_result.font_face.font_file.filename).resolve(), new_result.font_face.font_index))

            if (
                new_result is None
                or source_face != result.font_face
                or (new_result.mismatch_bold, new_result.need_faux_bold, new_result.mismatch_italic)
                != (result.mismatch_bold, result.need_faux_bold, result.mismatch_italic)
            ):
                problems.append(f"'{style.fontname}' in '{sub.file.name}'")

    if problems:
        raise error(f"Subsetting changed which fonts are used for {', '.join(problems)}! Please report this.", subset_fonts)


def _weight_to_name(weight: int) -> str | int:
    # https://learn.microsoft.com/en-us/typography/opentype/spec/os2#usweightclass
    match weight:
        case 100:
            return "Thin"
        case 200:
            return "ExtraLight"
        case 300:
            return "Light"
        case 400:
            return ""
        case 500:
            return "Medium"
        case 600:
            return "SemiBold"
        case 700:
            return "Bold"
        case 800:
            return "ExtraBold"
        case 900:
            return "Black"
    return weight


def _get_fontname(font: ABCFontFace) -> str:
    from font_collector import VariableFontFace

    filename_fallback = False
    try:
        found = font.get_family_name_from_lang("en")
        if not found:
            found = font.get_best_family_name()
        name = found.value
    except:
        # Not quite sure why this is nullable
        if not font.font_file:
            raise error(f"Could not find font file for '{font.get_best_exact_name()}'!")
        name = Path(font.font_file.filename).with_suffix("").name.strip()
        filename_fallback = True

    name = name.replace("/", " ").replace("\\", " ")

    if not filename_fallback:
        if " " in name:
            name = "".join([(part.capitalize() if part.islower() else part) for part in name.split(" ")])
        else:
            name = name.capitalize()
        if isinstance(font, VariableFontFace):
            name += "-VariableCollection"
            if font.is_italic:
                name += "Italic"
        else:
            weight = _weight_to_name(font.weight)
            if weight:
                name += f"-{weight}"
            if font.is_italic:
                name += ("" if weight else "-") + "Italic"

    return name


def collect_fonts(
    subs: SubFile | list[SubFile],
    use_system_fonts: bool = True,
    search_current_dir: bool = True,
    additional_fonts: PathLike | list[PathLike] = [],
    collect_draw_fonts: bool = True,
    use_ntfs_compliant_names: bool | None = None,
) -> list[MTFontFile]:
    """
    Collects the fonts used by one or more subtitles, loading the font files only once.
    For multiple subtitles, this is much faster than calling `SubFile.collect_fonts` on each one.
    The fonts are copied into the workdir of the current Setup, and every font in the workdir is returned.

    :param subs:                        The subtitle file or files to collect fonts for.
    :param use_system_fonts:            Search the fonts installed on the system.
    :param search_current_dir:          Recursively search the current working directory for fonts.
    :param additional_fonts:            Font files or directories to search, as a single path or a list.
    :param collect_draw_fonts:          Include fonts used only for drawings (usually Arial).
                                        See https://github.com/libass/libass/issues/617 for details.
    :param use_ntfs_compliant_names:    Ensure that filenames will work on NTFS (Windows) filesystems.
                                        The default `None` only does this when running on Windows.

    :return:                            A list of FontFile objects
    """
    if not isinstance(subs, list):
        subs = [subs]
    if not isinstance(additional_fonts, list):
        additional_fonts = [additional_fonts]

    return _collect_fonts(subs, use_system_fonts, search_current_dir, additional_fonts, collect_draw_fonts, False, use_ntfs_compliant_names)


def _collect_fonts(
    subs: list[SubFile],
    use_system_fonts: bool,
    search_current_dir: bool,
    additional_fonts: list[PathLike],
    collect_draw_fonts: bool,
    error_missing: bool,
    use_ntfs_compliant_names: bool | None,
) -> list[MTFontFile]:
    if search_current_dir:
        additional_fonts = [*additional_fonts, os.getcwd()]

    resolved_paths: list[Path] = []

    for f in additional_fonts:
        f = ensure_path_exists(f, collect_fonts, True)
        if f.is_dir():
            resolved_paths.extend([file for file in f.rglob("*.[tT][tT][fF]")])
            resolved_paths.extend([file for file in f.rglob("*.[oO][tT][fF]")])
            resolved_paths.extend([file for file in f.rglob("*.[tT][tT][cC]")])
            resolved_paths.extend([file for file in f.rglob("*.[oO][tT][cC]")])
        else:
            if f.suffix.lower() not in [".ttf", ".otf", ".ttc", ".otc"]:
                raise error(f"'{f.name}' is not a font!", collect_fonts)
            resolved_paths.append(f)

    if use_ntfs_compliant_names is None:
        use_ntfs_compliant_names = os.name == "nt"

    def clean_name(fontname: str) -> str:
        removed_slash = fontname.replace("/", "_")
        if not use_ntfs_compliant_names:
            return removed_slash
        return re.sub(r"[\<\>\*\\\:\|\?\"]", "_", removed_slash)

    from font_collector import set_loglevel

    set_loglevel(logging.CRITICAL)

    from font_collector import AssDocument, FontLoader, FontCollection, FontSelectionStrategyLibass, ABCFontFace, VariableFontFace

    font_collection = FontCollection(
        use_system_fonts, additional_fonts=FontLoader.load_additional_fonts(resolved_paths, scan_subdirs=True) if resolved_paths else []
    )
    load_strategy = FontSelectionStrategyLibass()

    found_fonts = list[MTFontFile]()
    # Track source path -> workdir copy so TTC files aren't duplicated per-face
    copied_sources: dict[Path, Path] = {}

    for sub in subs:
        info(f"Collecting fonts for '{sub.file.stem}'...", sub)
        doc = AssDocument(sub._read_doc())
        styles = doc.get_used_style(collect_draw_fonts)

        collected_faces = set[ABCFontFace]()

        for style, usage_data in styles.items():
            query = font_collection.get_used_font_by_style(style, load_strategy)

            if not query:
                msg = f"Font '{style.fontname}' was not found!"
                if error_missing:
                    raise error(msg, collect_fonts)
                else:
                    danger(msg, collect_fonts)
            else:
                fontname = _get_fontname(query.font_face)
                assert query.font_face.font_file
                fontpath = Path(query.font_face.font_file.filename)
                outpath = get_workdir() / f"{clean_name(fontname)}{fontpath.suffix}"
                family_name = query.font_face.get_best_family_name().value

                if isinstance(query.font_face, VariableFontFace):
                    outpath = outpath.with_suffix(".ttc")

                    if not outpath.exists():
                        info(f"Converting '{family_name}' variable font to a collection.")
                        query.font_face.variable_font_to_collection(outpath)
                elif query.font_face not in collected_faces:
                    # If this source file was already copied (e.g. another face from the
                    # same TTC), reuse the existing copy instead of creating a duplicate.
                    if fontpath in copied_sources:
                        outpath = copied_sources[fontpath]
                    else:
                        # A different font file may already be in the workdir under the same name, so use a numbered one then.
                        # The same file is reused, e.g. when collecting again.
                        base, counter = outpath, 1
                        while outpath.exists() and not filecmp.cmp(fontpath, outpath, shallow=False):
                            outpath = base.with_stem(f"{base.stem} ({counter})")
                            counter += 1
                        if not outpath.exists():
                            shutil.copy(fontpath, outpath)
                        copied_sources[fontpath] = outpath

                if query.font_face not in collected_faces:
                    info(f"Found font '{fontname}'.", collect_fonts)
                    collected_faces.add(query.font_face)

                if query.need_faux_bold:
                    warn(f"Faux bold used for '{fontname}' (requested weight {style.weight}, got {query.font_face.weight})!", collect_fonts)
                elif query.mismatch_bold:
                    warn(f"Mismatched weight for '{fontname}' (requested weight {style.weight}, got {query.font_face.weight})!", collect_fonts)

                if query.mismatch_italic:
                    warn(f"Could not find a requested {'non-' if query.font_face.is_italic else ''}italic variant for '{fontname}'!", collect_fonts)

                missing_glyphs = query.font_face.get_missing_glyphs(usage_data.characters_used)
                if len(missing_glyphs) != 0:
                    danger(f"'{fontname}' is missing the following glyphs: {missing_glyphs}", collect_fonts)

    for r in ["*.[tT][tT][fF]", "*.[oO][tT][fF]", "*.[tT][tT][cC]", "*.[oO][tT][cC]"]:
        for f in get_workdir().glob(r):
            found_fonts.append(MTFontFile(f))
    return found_fonts
