"""Command-line interface: ``bchdedupe scan`` and ``bchdedupe apply``."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from bchdedupe import core

if TYPE_CHECKING:
    from collections.abc import Sequence


def _size(text: str) -> int:
    try:
        return core.parse_size(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bchdedupe",
        description=(
            "Deduplicate files on bcachefs without hashing: FIDEDUPERANGE makes the "
            "filesystem itself compare the extents, so nothing is read or recomputed "
            "on the user side, and nothing is ever deleted."
        ),
        epilog=(
            "Typical use: bchdedupe scan /home --min-size 100M --out groups.json, then "
            "bchdedupe apply groups.json. Pass --dry-run to see the estimate without "
            "touching anything."
        ),
    )
    parser.add_argument("--version", action="version", version="bchdedupe 0.1.0")
    subcommands = parser.add_subparsers(dest="command", required=True)

    scan = subcommands.add_parser(
        "scan", help="find duplicate candidates (read-only, writes a group list)"
    )
    scan.add_argument("root", nargs="?", default=".", help="tree to scan")
    scan.add_argument(
        "--min-size",
        type=_size,
        default=100_000_000,
        help="ignore files smaller than this (default: 100M)",
    )
    scan.add_argument("--out", default="groups.json", help="where to write the group list")
    scan.add_argument("--jobs", type=int, default=8, help="fingerprinting threads")
    scan.add_argument(
        "--walk",
        choices=("auto", "fd", "python"),
        default="auto",
        help="'fd' is ~25x faster than the Python walk; 'auto' prefers it",
    )
    scan.add_argument(
        "--index",
        metavar="FILE",
        help="reuse a 'size|inode|path' listing instead of walking",
    )

    apply = subcommands.add_parser(
        "apply", help="share extents for the groups found by 'scan'"
    )
    apply.add_argument("groups", help="group list produced by 'scan'")
    apply.add_argument(
        "--dry-run",
        action="store_true",
        help="report the estimate without calling FIDEDUPERANGE",
    )
    apply.add_argument("--jobs", type=int, default=8, help="worker threads")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        found = core.scan(
            args.root,
            args.min_size,
            jobs=args.jobs,
            walk_mode=args.walk,
            index=args.index,
            log=_progress,
        )
        core.write_groups(found, args.out)
        _progress(f"wrote {args.out}")
        return 0

    _root, _min_size, groups = core.read_groups(args.groups)
    _progress(
        f"{len(groups)} groups, {core.human(sum(g.waste for g in groups))} at stake, "
        f"jobs={args.jobs}, dry_run={args.dry_run}"
    )
    if args.dry_run:
        estimate = core.apply_groups(groups, jobs=args.jobs, dry_run=True, log=_progress)
        _progress(
            f"{estimate.deduped} files would be deduplicated, "
            f"{core.human(estimate.gained)} at best"
        )
        return 0

    applied = core.apply_groups(groups, jobs=args.jobs, log=_progress)
    _progress("")
    _progress(
        f"{applied.deduped} files deduplicated, {core.human(applied.gained)} reclaimed "
        f"in {applied.elapsed:.1f}s ({core.human(applied.rate)}/s)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
