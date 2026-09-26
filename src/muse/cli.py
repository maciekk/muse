"""Muse command-line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from functools import partial

from rich_argparse import RichHelpFormatter

from muse import __version__
from muse.commands.compact import _compact
from muse.commands.content import _import, _move, _slag
from muse.commands.duplicates import _diff, _dupes, _prune
from muse.commands.overview import _doctor, _search, _stats, _status
from muse.commands.scan import _scan
from muse.config import resolve_root
from muse.reporting import (
    make_console,
)

_NEGATED_SEARCH_TERM = "muse-internal-negated-search-term:"


class MuseHelpFormatter(RichHelpFormatter):
    """Rich help with prominent command and option names."""

    styles = {
        **RichHelpFormatter.styles,
        "argparse.args": "bold cyan",
        "argparse.groups": "bold",
        "argparse.metavar": "cyan",
    }

    def _rich_format_action(self, action: argparse.Action):
        """Avoid repeating the subparser metavar under the Commands heading."""
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for subaction in self._iter_indented_subactions(action):
                yield from self._rich_format_action(subaction)
            return
        yield from super()._rich_format_action(action)


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _add_max_threads_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-threads",
        type=_positive_int,
        metavar="N",
        help="limit concurrent hashing and scanning workers (default: up to 16)",
    )


def build_parser(color: str = "auto") -> argparse.ArgumentParser:
    formatter = partial(MuseHelpFormatter, console=make_console(color))
    parser = argparse.ArgumentParser(
        prog="muse",
        description="Curate and publish a personal music archive.",
        formatter_class=formatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="library root (default: ~/music-vault)",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="terminal color policy (default: auto)",
    )

    commands = parser.add_subparsers(
        dest="command",
        title="Commands",
        metavar="COMMAND",
        parser_class=partial(argparse.ArgumentParser, formatter_class=formatter),
    )

    def command(name: str, summary: str) -> argparse.ArgumentParser:
        description = f"{summary[:1].upper()}{summary[1:].removesuffix('.')}."
        return commands.add_parser(name, help=summary, description=description)

    doctor = command("doctor", "inspect repository layout and supporting tools")
    _add_json_argument(doctor)
    doctor.set_defaults(handler=_doctor)

    status = command("status", "show top-level repository status")
    _add_json_argument(status)
    status.set_defaults(handler=_status)

    stats = command("stats", "report file counts and disk usage")
    stats.add_argument(
        "target",
        nargs="?",
        help="path to inspect; relative paths are resolved beneath the library root",
    )
    _add_json_argument(stats)
    stats.set_defaults(handler=_stats)

    scan = command("scan", "check whether an external tree contains files absent from the vault")
    scan.add_argument("target", help="external file or directory to inspect")
    scan.add_argument(
        "--thorough",
        action="store_true",
        help="compare SHA-256 checksums instead of filenames and sizes",
    )
    scan.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="target scan progress display policy (default: auto)",
    )
    scan.add_argument(
        "--all",
        action="store_true",
        help="show every not-found directory and file",
    )
    scan.add_argument(
        "--pull",
        action="store_true",
        help="interactively copy directories containing not-found audio into backlog",
    )
    _add_max_threads_argument(scan)
    _add_json_argument(scan)
    scan.set_defaults(handler=_scan)

    search = command("search", "find files or directories by name across the vault")
    search.add_argument(
        "terms",
        nargs="+",
        metavar="QUERY",
        help="case-insensitive terms; prefix with - to exclude matching paths",
    )
    _add_json_argument(search)
    search.set_defaults(handler=_search)

    dupes = command("dupes", "find byte-identical files; names and locations need not match")
    dupes.add_argument(
        "targets",
        nargs="*",
        help="paths to inspect; default scans the vault except slag/ and trash/",
    )
    dupes.add_argument(
        "--rehash",
        action="store_true",
        help="recompute every hash instead of using unchanged cached entries",
    )
    dupes.add_argument(
        "--trees",
        action="store_true",
        help="report maximal byte-identical directory trees (hashes every file)",
    )
    dupes.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="progress display policy (default: auto)",
    )
    dupes.add_argument(
        "--all",
        action="store_true",
        help="show every duplicate group instead of the 10 largest",
    )
    _add_max_threads_argument(dupes)
    _add_json_argument(dupes)
    dupes.set_defaults(handler=_dupes)

    prune = command("prune", "remove missing files from the hash cache")
    _add_json_argument(prune)
    prune.set_defaults(handler=_prune)

    tree_diff = command("diff", "compare two directory trees exactly")
    tree_diff.add_argument("left", help="first tree; relative paths are beneath the library root")
    tree_diff.add_argument("right", help="second tree; relative paths are beneath the library root")
    _add_json_argument(tree_diff)
    tree_diff.set_defaults(handler=_diff)

    compact = command("compact", "plan exact duplicate-tree compaction")
    compact.add_argument(
        "target",
        nargs="?",
        help="path to compact; relative paths are resolved beneath the library root",
    )
    compact_actions = compact.add_mutually_exclusive_group()
    compact_actions.add_argument("--show", action="store_true", help="show the pending plan")
    compact_actions.add_argument("--apply", action="store_true", help="apply the pending plan")
    compact.add_argument(
        "--prefer",
        action="append",
        default=[],
        metavar="PATH",
        help="prefer retaining copies beneath PATH; repeat in priority order",
    )
    compact.add_argument(
        "--all",
        action="store_true",
        help="show every planned trash move",
    )
    _add_max_threads_argument(compact)
    compact.set_defaults(handler=_compact)

    command("help", "show help for Muse or one command")

    import_command = command(
        "import", "strictly validate, plan, or apply a release or single import into master"
    )
    import_command.add_argument(
        "source",
        nargs="?",
        help="file or directory beneath backlog (not needed to show, apply, or abort a plan)",
    )
    import_command.add_argument(
        "destination",
        nargs="?",
        help="destination beneath master; omitted to suggest one from tags",
    )
    import_actions = import_command.add_mutually_exclusive_group()
    import_actions.add_argument("--apply", action="store_true", help="apply the current plan")
    import_actions.add_argument("--abort", action="store_true", help="discard the current plan")
    _add_json_argument(import_command)
    import_command.set_defaults(handler=_import)

    relocation = command("mv", "move content and preserve cached hashes")
    relocation.add_argument("source", help="existing path beneath the library root")
    relocation.add_argument("destination", help="new path beneath the library root")
    _add_json_argument(relocation)
    relocation.set_defaults(handler=_move)

    slag = command("slag", "inspect or move non-audio backlog artifacts")
    slag.add_argument(
        "sources",
        nargs="*",
        metavar="PATH",
        help="backlog files or directories to move into slag",
    )
    slag.add_argument("--long", action="store_true", help="include size and file type")
    slag.add_argument("--all", action="store_true", help="list every selected file")
    slag.add_argument("--dirs", action="store_true", help="list directories only")
    slag.add_argument(
        "--thorough",
        action="store_true",
        help="also select audio-adjacent artwork, metadata, and checksum files",
    )
    slag.add_argument(
        "--apply", action="store_true", help="move the selected artifacts after confirmation"
    )
    _add_json_argument(slag)
    slag.set_defaults(handler=_slag)

    return parser


def _protect_negated_search_terms(argv: list[str]) -> list[str]:
    """Keep search's ``-term`` shorthand from being parsed as an option."""
    arguments = list(argv)
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--root", "--color"}:
            index += 2
            continue
        if argument.startswith(("--root=", "--color=")) or argument.startswith("-"):
            index += 1
            continue
        if argument != "search":
            return arguments

        for term_index in range(index + 1, len(arguments)):
            term = arguments[term_index]
            if term == "--":
                break
            is_negated = len(term) > 1 and term.startswith("-") and not term.startswith("--")
            if is_negated and term != "-h":
                arguments[term_index] = f"{_NEGATED_SEARCH_TERM}{term[1:]}"
        return arguments
    return arguments


def _help_color(argv: Sequence[str]) -> str:
    """Read the color preference before argparse can handle an early --help."""
    for index, argument in enumerate(argv):
        if argument.startswith("--color="):
            return argument.partition("=")[2]
        if argument == "--color" and index + 1 < len(argv):
            return argv[index + 1]
    return "auto"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["help"]:
        arguments = [*arguments[1:], "--help"] if len(arguments) > 1 else ["--help"]
    arguments = _protect_negated_search_terms(arguments)
    parser = build_parser(_help_color(arguments))
    try:
        args = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "search":
        args.terms = [
            f"-{term.removeprefix(_NEGATED_SEARCH_TERM)}"
            if term.startswith(_NEGATED_SEARCH_TERM)
            else term
            for term in args.terms
        ]
    root = resolve_root(args.root)
    return args.handler(args, root)
