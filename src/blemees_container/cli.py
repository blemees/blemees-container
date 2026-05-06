"""Command-line entry point for ``blemees-container``.

The v0.1.0 release ships the argparse shell only — every subcommand
prints a "not implemented yet" stub. The actual lifecycle logic
arrives in subsequent releases per ``docs/IMPLEMENTATION_PLAN.md``.
The CLI surface is fixed by ``docs/SPEC.md`` §4 so that downstream
consumers (TUI, scripts) can pin against it without churn.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from blemees_container._version import __version__

_NOT_YET = (
    "blemees-container {cmd}: not implemented in v{ver}. "
    "Track progress in docs/IMPLEMENTATION_PLAN.md."
)


def _stub(cmd: str) -> int:
    print(_NOT_YET.format(cmd=cmd, ver=__version__), file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blemees-container",
        description="Run blemees-agentd inside a sandboxed container.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"blemees-container {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    p_start = sub.add_parser("start", help="Start a containerized agentd")
    p_start.add_argument("name", help="Container name (alias-grammar)")
    p_start.add_argument("--image", help="Container image reference")
    p_start.add_argument("--container-home", help="Image's $HOME (e.g. /root)")
    p_start.add_argument("--home", help="Project dir on the host")
    p_start.add_argument("--workdir", help="Working directory inside the container")
    p_start.add_argument("--alias", help="Sets BLEMEES_AGENT_ALIAS")
    p_start.add_argument(
        "--auth",
        action="append",
        default=[],
        metavar="PROVIDER",
        help="Access-only auth copy-in (claude|codex; repeatable)",
    )
    p_start.add_argument(
        "--auth-file",
        action="append",
        default=[],
        metavar="HOST:CONTAINER[:ro]",
        help="Verbatim auth file copy-in (repeatable)",
    )
    p_start.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume an existing session from the host pool",
    )
    p_start.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional env (repeatable)",
    )
    p_start.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="HOST:CONTAINER[:ro]",
        help="Additional bind-mounts (repeatable)",
    )
    p_start.add_argument(
        "--engine",
        choices=("docker", "podman", "nerdctl"),
        help="Container engine (default: auto-detect)",
    )
    p_start.add_argument(
        "--as-root",
        action="store_true",
        help="Skip --user mapping; image needs it",
    )

    p_stop = sub.add_parser("stop", help="Stop a containerized agentd")
    p_stop.add_argument("name", help="Container name")
    p_stop.add_argument("--no-capture", action="store_true", help="Skip session state copy-out")
    p_stop.add_argument("--timeout", type=float, default=30.0, help="SIGTERM grace (seconds)")
    p_stop.add_argument("--force", action="store_true", help="SIGKILL immediately")
    p_stop.add_argument("--rm", action="store_true", help="Remove after stop")

    sub.add_parser("list", help="List known containers and their sessions")

    p_exec = sub.add_parser("exec", help="Run a command inside a container")
    p_exec.add_argument("name", help="Container name")
    p_exec.add_argument("argv", nargs=argparse.REMAINDER, help="Command and args")

    p_logs = sub.add_parser("logs", help="Stream container logs")
    p_logs.add_argument("name", help="Container name")
    p_logs.add_argument("--follow", action="store_true", help="Stream live")
    p_logs.add_argument(
        "--source",
        choices=("engine", "agentd", "container"),
        default="engine",
        help="Log source (default: engine)",
    )

    p_rm = sub.add_parser("rm", help="Remove a stopped container")
    p_rm.add_argument("name", help="Container name")
    p_rm.add_argument("--force", action="store_true", help="Stop first if running")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 0
    return _stub(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
