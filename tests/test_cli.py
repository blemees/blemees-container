"""Smoke tests for the CLI scaffold.

Until the actual lifecycle logic lands, these tests just pin down
the public surface: --version prints the package version, --help
runs cleanly, every documented subcommand parses, and stubs exit
with ``EXIT_NOT_IMPLEMENTED`` (69) and a recognizable marker, distinct
from argparse's usage-error code 2.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from blemees_container import __version__
from blemees_container.cli import EXIT_NOT_IMPLEMENTED, main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("start", "stop", "list", "exec", "logs", "rm"):
        assert cmd in out


def test_no_args_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage:" in out


@pytest.mark.parametrize(
    "argv",
    [
        [
            "start",
            "scratch",
            "--image",
            "blemees:latest",
            "--container-home",
            "/root",
            "--home",
            "/tmp/p",
        ],
        ["stop", "scratch"],
        ["list"],
        ["exec", "scratch", "--", "echo", "hi"],
        ["logs", "scratch", "--follow"],
        ["rm", "scratch"],
    ],
)
def test_subcommand_stub_exits_with_marker(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(argv)
    assert rc == EXIT_NOT_IMPLEMENTED
    assert rc != 2
    err = capsys.readouterr().err
    assert "not implemented" in err
    assert __version__ in err


@pytest.mark.parametrize(
    "argv",
    [
        ["stop"],
        ["logs", "x", "--source", "bogus"],
    ],
)
def test_bad_arguments_exit_with_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert exc.value.code != EXIT_NOT_IMPLEMENTED
    assert "usage:" in capsys.readouterr().err


def test_console_script_installed() -> None:
    """The ``blemees-container`` console script must run end-to-end
    (entry-point wiring intact)."""
    res = subprocess.run(
        [sys.executable, "-m", "blemees_container.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert __version__ in res.stdout
