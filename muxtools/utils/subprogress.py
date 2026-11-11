from typing import Any, Sequence
from math import ceil
from datetime import timedelta
from re import Pattern, compile
from dataclasses import dataclass
from shlex import split as splitcmd
from subprocess import Popen, PIPE, STDOUT

from .convert import timedelta_from_formatted

__all__ = ["ProgressBarConfig", "run_cmd_pb", "run_mkvtoolnix_pb", "PERCENTAGE_PATTERN", "FFMPEG_TIME_PATTERN", "MKVTOOLNIX_PATTERN"]

PERCENTAGE_PATTERN = compile(r"(\d+(?:\.\d+)?)%")
FFMPEG_TIME_PATTERN = compile(r"time=(\d+:\d+:\d+.\d+)")
# Progress output of the mkvtoolnix tools with --gui-mode, which isn't translated like their regular output
MKVTOOLNIX_PATTERN = compile(r"#GUI#progress (\d+)%")


@dataclass
class ProgressBarConfig:
    description: str = ""
    target: int | timedelta = 100
    regex: Pattern | str | None = None
    groupnum: int = 1
    success_return: int | Sequence[int] = 0


def run_cmd_pb(cmd: str | list[str], silent: bool = True, pbc: ProgressBarConfig | None = None, **kwargs: Any) -> int:
    """
    Runs a command and shows a progress bar based on its output.

    :param cmd:         The command to run
    :param silent:      Hides the output of the command. It is still printed if the command fails.
    :param pbc:         Configuration for parsing the progress from the output
    :param kwargs:      Passed on to Popen

    :return:            The return code of the command
    """
    from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, TimeElapsedColumn

    pbc = pbc or ProgressBarConfig()
    args = splitcmd(cmd) if isinstance(cmd, str) else cmd
    process = Popen(args, stdout=PIPE, stderr=STDOUT, text=True, encoding="UTF-8", errors="ignore", bufsize=1, **kwargs)
    regex = pbc.regex or (PERCENTAGE_PATTERN if isinstance(pbc.target, int) else FFMPEG_TIME_PATTERN)
    if isinstance(regex, str):
        regex = compile(regex)
    success = [pbc.success_return] if isinstance(pbc.success_return, int) else list(pbc.success_return)

    output = list[str]()
    with Progress(
        TextColumn("[progress.description]{task.description}"), BarColumn(), TaskProgressColumn(), TimeRemainingColumn(), TimeElapsedColumn()
    ) as pro:
        task = pro.add_task(pbc.description)
        prev = 0
        assert process.stdout
        # Universal newlines also split on the carriage returns most tools use to redraw their progress
        for line in iter(process.stdout.readline, ""):
            output.append(line)
            if not silent:
                print(line, end="" if line.endswith("\n") else "\n")

            line = line.strip()
            matches = regex.search(line)

            if matches:
                if isinstance(pbc.target, int):
                    val = float(matches.group(pbc.groupnum))
                    rounded = round(val)
                    if prev < rounded:
                        pro.update(task, completed=rounded)
                        prev = rounded
                else:
                    current_string: str = matches[pbc.groupnum]
                    if current_string.count(":") == 1:
                        current_string = f"0:{current_string}"
                    current = timedelta_from_formatted(current_string)
                    percentage = current.total_seconds() / pbc.target.total_seconds() * 100
                    if prev < percentage:
                        pro.update(task, completed=ceil(percentage))
                        prev = percentage

        process.wait()
        if process.returncode in success:
            pro.update(task, completed=100)
        pro.stop()

    if process.returncode not in success and silent and (lines := "".join(output).strip()):
        print("\n----------------------")
        print(lines)
        print("----------------------")

    return process.returncode


def run_mkvtoolnix_pb(cmd: list[str], description: str, quiet: bool = True) -> int:
    """
    Runs a mkvtoolnix command, showing a progress bar if quiet and the regular output otherwise.

    :param cmd:         The command to run, starting with the executable
    :param description: Description shown next to the progress bar
    :param quiet:       Shows a progress bar instead of the output of the command. The output is still printed on failure.

    :return:            The return code of the command. 1 means there were warnings.
    """
    from .env import run_commandline

    if not quiet:
        return run_commandline(cmd, False, mkvmerge=True)

    return run_cmd_pb([cmd[0], "--gui-mode", *cmd[1:]], True, ProgressBarConfig(description, regex=MKVTOOLNIX_PATTERN, success_return=[0, 1]))
