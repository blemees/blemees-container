"""Parser tests for the CLI surface fixed by SPEC §4.

These pin down what the parser accepts and rejects: the three
required ``start`` flags, ``start``'s trailing ``-- <agent-cmd> ...``
and ``list --json``. They do not check the stub exit code.
"""

from __future__ import annotations

import pytest

from blemees_container import cli
from blemees_container.cli import _build_parser, main

START = [
    "start",
    "scratch",
    "--image",
    "blemees:latest",
    "--container-home",
    "/root",
    "--home",
    "/tmp/p",
]


def _without(argv: list[str], flag: str) -> list[str]:
    i = argv.index(flag)
    return argv[:i] + argv[i + 2 :]


@pytest.mark.parametrize("flag", ["--image", "--container-home", "--home"])
def test_start_required_flag_missing_is_usage_error(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(_without(START, flag))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert flag in err


def test_start_double_dash_is_not_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    main([*START, "--", "claude", "--model", "x"])
    assert "usage:" not in capsys.readouterr().err


def test_start_agent_cmd_after_double_dash() -> None:
    args = cli._parse_args(_build_parser(), [*START, "--", "claude", "--model", "x"])
    assert args.cmd == "start"
    assert args.agent_cmd == ["claude", "--model", "x"]
    assert args.image == "blemees:latest"


def test_start_without_double_dash_has_empty_agent_cmd() -> None:
    args = cli._parse_args(_build_parser(), START)
    assert args.agent_cmd == []


def test_start_empty_agent_cmd_after_double_dash() -> None:
    args = cli._parse_args(_build_parser(), [*START, "--"])
    assert args.agent_cmd == []


def test_start_options_after_double_dash_are_not_start_options() -> None:
    args = cli._parse_args(
        _build_parser(),
        [*START, "--", "--image", "other", "--resume", "sess_x", "--as-root"],
    )
    assert args.image == "blemees:latest"
    assert args.resume is None
    assert args.as_root is False
    assert args.agent_cmd == ["--image", "other", "--resume", "sess_x", "--as-root"]


def test_start_required_flag_only_after_double_dash_is_usage_error() -> None:
    argv = [*_without(START, "--image"), "--", "--image", "blemees:latest"]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        [*START, "claude"],
        [*START, "claude", "--", "x"],
    ],
)
def test_start_stray_positional_is_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "claude" in capsys.readouterr().err


def test_exec_keeps_its_own_argv() -> None:
    args = cli._parse_args(_build_parser(), ["exec", "start", "--", "echo", "--", "hi"])
    assert args.cmd == "exec"
    assert args.name == "start"
    assert "agent_cmd" not in vars(args)


@pytest.mark.parametrize(("argv", "expected"), [(["list", "--json"], True), (["list"], False)])
def test_list_json_flag(argv: list[str], expected: bool) -> None:
    args = _build_parser().parse_args(argv)
    assert args.cmd == "list"
    assert args.json is expected


def test_start_help_shows_agent_cmd(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["start", "--help"])
    assert exc.value.code == 0
    assert "-- <agent-cmd>" in capsys.readouterr().out
