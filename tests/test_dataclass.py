import pytest

from muxtools.utils.dataclass import CLIKwargs, allow_extra, dataclass


@dataclass(config=allow_extra)
class EmptyArgs(CLIKwargs):
    pass


@dataclass(config=allow_extra)
class ConfiguredArgs(CLIKwargs):
    value: int = 1


@pytest.mark.parametrize("args_type", [EmptyArgs, ConfiguredArgs])
def test_custom_args_updates(args_type: type[EmptyArgs] | type[ConfiguredArgs]) -> None:
    args = args_type()
    args.update_custom_args(bitrate=192)
    args.update_custom_args(bitrate=256, encoder="opus")

    assert args.get_custom_args_dict() == {"bitrate": 256, "encoder": "opus"}
    assert args.get_custom_args() == ["--bitrate", "256", "--encoder", "opus"]


@pytest.mark.parametrize("affinity", [0.25, 0.5, 1.0, 2.0])
def test_process_affinity_fraction(affinity: float) -> None:
    from math import ceil
    from os import cpu_count

    @dataclass(config=allow_extra)
    class AffinityArgs(CLIKwargs):
        affinity: float = 1.0

    threads = AffinityArgs(affinity=affinity).get_process_affinity()

    assert isinstance(threads, list)
    assert len(threads) == ceil((cpu_count() or 1) * min(affinity, 1.0))
