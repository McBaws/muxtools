from shlex import join as joincommand
from shutil import rmtree
from pathlib import Path
from typing import Sequence
import subprocess
import re


from .tmdb import TmdbConfig
from .muxfiles import MuxingFile
from .token_handling import TOKEN_REGEX, apply_dynamic_tokens, replace_with_temp_tokens
from ..utils.types import PathLike
from ..subtitle.sub import FontFile
from ..utils.glob import GlobSearch
from ..misc.chapters import Chapters
from .tracks import Attachment, _track
from ..utils.download import download_file, get_executable
from ..utils.log import debug, error, info, warn, danger
from ..utils.env import get_setup_attr, get_setup_dir, get_workdir, get_temp_workdir, run_commandline
from ..utils.files import ensure_path, ensure_path_exists, get_crc32, clean_temp_files
from ..utils.probe import ParsedFile

__all__ = ["mux"]


writing_lib_regex = re.compile(r"libebml\s*v(\d+(?:\.\d+)+).+?libmatroska\s*v(\d+(?:\.\d+)+)", re.I)
statistics_writer_regex = re.compile(r"\s*<Simple>\s*<Name>_STATISTICS_WRITING_(?:APP|DATE_UTC)</Name>.*?</Simple>", re.S)


def mux(
    *tracks: _track | MuxingFile | Chapters | PathLike | GlobSearch | None,
    tmdb: TmdbConfig | None = None,
    outfile: PathLike | None = None,
    quiet: bool = True,
    print_cli: bool = False,
    anonymize: bool = False,
) -> Path:
    """
    Runs the mux.

    :param *tracks:     Any amount of track objects, MuxingFiles and Chapters objects.\n
                        Paths to txt or xml chapter files are also accepted and `None` values are skipped.
    :param tmdb:        A TMDB Config used for additional tagging if you so desire.
    :param outfile:     If you want to overwrite the output file path
    :param quiet:       Whether or not to print the mkvmerge output
    :param print_cli:   Print the final muxing command before running it if True
    :param anonymize:   Removes identifying metadata from the muxed file: the muxing date, the muxing and writing application
                        and the application/date entries of the track statistics tags. The statistics themselves are kept.\n
                        This also skips the muxtools branding.
                        Encoder information that is part of the streams themselves (like x264/x265 settings) is not touched.

    :return:            Path to the muxed file
    """
    check_mkvmerge_version()
    tracklist = list(tracks)
    out_dir = ensure_path(get_setup_attr("out_dir", "premux"), "Mux")
    args: list[str] = [get_executable("mkvmerge")]

    filename, mkvtitle = output_names(tmdb, args, tracklist)

    if not outfile:
        if filename.lower().endswith(".mkv"):
            outfile = Path(out_dir, filename)
        else:
            outfile = Path(out_dir, f"{filename}.mkv")

    outfile = ensure_path(outfile, "Mux")

    args.extend(["-o", str(outfile)])

    for track in tracklist:
        if isinstance(track, _track):
            args.extend(track.mkvmerge_args())
            continue
        elif isinstance(track, MuxingFile):
            if not isinstance(track, FontFile):
                warn("It's strongly recommended to pass tracks to ensure naming and tagging instead of MuxingFiles directly!", "Mux")
            track = track.to_track()
            args.extend(track.mkvmerge_args())
            continue
        elif isinstance(track, Chapters):
            if not track.chapters:
                warn("Chapters are None or empty!", "Mux")
                continue

            args.extend(["--chapters", str(track.to_file())])
            continue
        elif isinstance(track, Path) or isinstance(track, str) or isinstance(track, GlobSearch):
            # Failsave for if someone passes Chapters().to_file() or a txt/xml file
            track = ensure_path_exists(track, "Mux")
            if track.suffix.lower() in [".txt", ".xml"]:
                args.extend(["--chapters", str(track.resolve())])
                continue
        elif track is None:
            continue

        raise error("Only _track, MuxingFiles or Chapters types are supported as muxing input!", "Mux")

    # Dynamic tokens can only be resolved from the muxed file, so such titles are set afterwards
    title_has_tokens = bool(TOKEN_REGEX.search(mkvtitle))
    if mkvtitle and not title_has_tokens:
        args.extend(["--title", mkvtitle])

    if print_cli:
        info(joincommand(args), "Mux")

    debug("Running the mux...", "Mux")
    if run_commandline(args, quiet, mkvmerge=True) > 1:
        raise error("Muxing failed!", "Mux")

    brand = not anonymize and not get_setup_attr("skip_mux_branding", False)
    parsed = ParsedFile.from_file(outfile, "Mux") if brand or title_has_tokens or TOKEN_REGEX.search(outfile.name) else None
    if parsed and title_has_tokens:
        mkvtitle = apply_dynamic_tokens(mkvtitle, parsed, True, caller="Mux")

    edit_file(outfile, parsed if brand else None, mkvtitle if title_has_tokens else None, anonymize)

    new_name = apply_dynamic_tokens(outfile.name, parsed or outfile, True, caller="Mux")
    if outfile.name != new_name:
        outfile = outfile.replace(outfile.parent / new_name)

    # This has to stay last because every change to the file changes the checksum
    if "#crc32#" in outfile.stem:
        debug("Generating CRC32 for the muxed file...", "Mux")
        outfile = outfile.replace(outfile.with_stem(outfile.stem.replace("#crc32#", get_crc32(outfile))))

    clean_temp_files()

    if get_setup_attr("clean_work_dirs", False):
        clean_workdir(outfile)

    debug("Done", "Mux")
    return outfile


def edit_file(outfile: Path, parsed: ParsedFile | None, title: str | None, anonymize: bool) -> None:
    """
    Applies every post-mux change to the file in a single mkvpropedit run.

    :param outfile:     The muxed file
    :param parsed:      The parsed muxed file, used for the muxtools branding. None skips the branding.
    :param title:       Title to set or None to leave it untouched
    :param anonymize:   Whether to remove identifying metadata
    """
    info_args: list[str] = []
    tag_args: list[str] = []

    if title is not None:
        info_args.extend(["--set", f"title={title}"])

    if anonymize:
        info_args.extend(["--delete", "date", "--set", "muxing-application=", "--set", "writing-application="])
        if tags_file := statistics_tags_without_writer(outfile):
            tag_args.extend(["--tags", f"all:{tags_file}"])
    elif parsed:
        info_args.extend(branding_args(parsed))

    if not info_args and not tag_args:
        return

    mkvpropedit = get_executable("mkvpropedit", can_error=False)
    if not mkvpropedit:
        if title is not None or anonymize:
            raise error("Mkvpropedit could not be found!", "Mux")
        warn("Mkvpropedit could not be found!", "Mux")
        return

    args = [mkvpropedit, str(outfile.resolve()), *tag_args]
    if info_args:
        args.extend(["--edit", "info", *info_args])

    if run_commandline(args, mkvmerge=True) > 1:
        if title is not None or anonymize:
            raise error("Failed to edit the muxed file via mkvpropedit!", "Mux")
        danger("Failed to add muxtools information via mkvpropedit!", "Mux")


def branding_args(parsed: ParsedFile) -> list[str]:
    from muxtools import __version__

    muxing_application = parsed.container_info.tags.get("encoder", None)
    if not muxing_application or not (match := writing_lib_regex.search(muxing_application)):
        debug("File does not contain writing library tags. Skipping the muxtools branding.", "Mux")
        return []

    muxing_application = f"libebml v{match.group(1)} + libmatroska v{match.group(2)} + muxtools v{__version__}"
    return ["--set", f"muxing-application={muxing_application}"]


def statistics_tags_without_writer(outfile: Path) -> Path | None:
    """
    Extracts all tags of the file and drops the application and date entries mkvmerge adds to the track statistics.

    :return:    Path to the new tags file or None if the file has no tags
    """
    tags_file = Path(get_temp_workdir(), f"{outfile.stem}_tags.xml")
    args = [get_executable("mkvextract"), str(outfile.resolve()), "tags", str(tags_file)]
    if run_commandline(args, mkvmerge=True) > 1:
        raise error("Failed to extract tags from the muxed file!", "Mux")

    # mkvextract doesn't create a file at all if there are no tags
    if not tags_file.exists():
        return None

    content = tags_file.read_text("utf-8")
    tags_file.write_text(statistics_writer_regex.sub("", content), "utf-8")
    return tags_file


def clean_workdir(outfile: Path) -> None:
    workdir = get_workdir().resolve()
    if not workdir.exists():
        return

    if Path.cwd().resolve().is_relative_to(workdir):
        error("Clearing workdir not supported when your current directory is inside of it.", "Mux")
        return

    if outfile.resolve().is_relative_to(workdir):
        danger("Not clearing the workdir because the muxed file is inside of it.", "Mux")
        return

    rmtree(workdir)


def clean_name(name: str, keep: Sequence[str] = ()) -> str:
    """
    This removes every unused token and delimiter aswell as empty brackets/parentheses.
    Ideally should be called before inserting the show name to not cause false positives with that.
    The .hack series would definitely cause some :)

    :param name:    The name to clean
    :param keep:    Additional tokens that should not be removed
    """
    stripped = name.strip()

    dont_match = [R"$show$", R"$ep$", R"$crc32$", *keep]
    for match in re.findall(r"\$[^ ]+?\$", name):
        if match not in dont_match:
            stripped = stripped.replace(match, "").strip()
            warn(f"Unknown token '{match}' was removed.", "Mux")

    stripped = re.sub(r"\s*(?:\(\)|\[\])", "", stripped).strip()

    delimiters = ["-", "/"]
    if not stripped.endswith("..."):
        delimiters.append(".")

    while any([stripped.startswith(delim) for delim in delimiters]):
        stripped = stripped.lstrip("".join(delimiters)).strip()

    while any([stripped.endswith(delim) for delim in delimiters]):
        stripped = stripped.rstrip("".join(delimiters)).strip()

    return stripped


def output_names(
    tmdb: TmdbConfig | None = None,
    args: list[str] | None = None,
    tracks: list[_track | MuxingFile | Chapters | PathLike | GlobSearch | None] | None = None,
) -> tuple[str, str]:
    args = args if args is not None else []
    tracks = tracks if tracks is not None else []

    show_name = get_setup_attr("show_name", "Example")
    episode = get_setup_attr("episode", "01")
    filename = get_setup_attr("out_name", R"$show$ - $ep$ (premux)")
    title = get_setup_attr("mkv_title_naming", "")

    if R"$crc32$" in title:
        raise error("The CRC32 can't be part of the mkv title because the title changes the checksum.", "Mux")

    epint = 0
    try:
        ep_temp = str(episode)
        if " " in ep_temp:
            ep_temp = ep_temp.split(" ")[0]
        epint = int(ep_temp)
    except:
        if tmdb and not tmdb.movie:
            danger(f"{episode} is not a valid integer! TMDB will be skipped.", "Mux")
            tmdb = None
            filename = clean_name(filename.replace(R"$title$", ""))
            title = clean_name(title.replace(R"$title$", ""))

    # Episode titles get inserted after cleaning so they can't be mangled by it
    episode_titles = dict[str, str]()

    if tmdb:
        debug("Fetching tmdb metadata...", "Mux")
        mediameta = tmdb.get_media_meta()
        epmeta = tmdb.get_episode_meta(epint) if not tmdb.movie else None
        if tmdb.needs_xml():
            xml = tmdb.make_xml(mediameta, epmeta)
            args.extend(["--global-tags", str(xml)])

        if not tmdb.movie and epmeta:
            if tmdb.write_cover and epmeta.thumb_url:
                cover = Path(get_workdir(), f"cover_land{Path(epmeta.thumb_url).suffix}")
                download_file(epmeta.thumb_url, cover)
                tracks.append(Attachment(cover, "image/jpeg" if cover.suffix.lower() == ".jpg" else "image/png"))

            episode_titles = {R"$title$": epmeta.title, R"$title_sanitized$": epmeta.title_sanitized}

    # Empty titles are removed before cleaning so their leftover delimiters get cleaned up as well
    for token, value in episode_titles.items():
        if not value:
            filename = filename.replace(token, "")
            title = title.replace(token, "")

    filename = replace_with_temp_tokens(filename)
    title = replace_with_temp_tokens(title)

    for attribute in get_setup_dir():
        attr = get_setup_attr(attribute, None)
        if not attr or not isinstance(attr, str):
            continue
        replace = Rf"${str(attribute)}$"
        filename = filename.replace(replace, attr)
        title = title.replace(replace, attr)

    keep = [token for token, value in episode_titles.items() if value]
    filename = clean_name(filename, keep)
    title = clean_name(title, keep)

    for token in keep:
        filename = filename.replace(token, episode_titles[token])
        title = title.replace(token, episode_titles[token])

    filename = filename.replace(R"$show$", show_name)
    filename = filename.replace(R"$ep$", episode)
    filename = filename.replace(R"$crc32$", "#crc32#")

    title = title.replace(R"$show$", show_name)
    title = title.replace(R"$ep$", episode)

    return (filename, title)


def check_mkvmerge_version():
    out = subprocess.run([get_executable("mkvmerge"), "--version"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    output = ((out.stderr or "") + (out.stdout or "")).strip()
    version_regex = re.compile(r".*mkvmerge v(?P<version>\d+(?:\.\d+)?).*", re.I)
    match = version_regex.match(output)
    try:
        if match:
            version = match.group("version")
            if version and float(version) < 77:
                danger("Please update your mkvtoolnix/mkvmerge for optimal behavior.")
    except:
        pass
