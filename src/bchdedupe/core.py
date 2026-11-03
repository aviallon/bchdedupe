"""Core primitives: candidate discovery and extent sharing.

Two facts drive the whole design, both verified against bcachefs 1.39.5:

1. ``FIDEDUPERANGE`` compares the data itself — through the extent checksums
   bcachefs already stores — and refuses (``deduped == 0``) when the contents
   differ. So there is no reason to hash anything in user space.
2. bcachefs does not implement ``FIEMAP``, so a tool that needs extent maps
   cannot work here at all. See ``docs/why-not-duperemove.md``.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import struct
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final

#: ``_IOWR(0x94, 54, struct file_dedupe_range)`` — the whole point of the tool.
FIDEDUPERANGE: Final[int] = 0xC0189436

#: Chunk size for the ioctl. Measured optimum on bcachefs: 16 MiB gives ~2.3 GB/s
#: on contiguous data, where a single 512 MiB request takes 2.6 s.
DEDUPE_CHUNK: Final[int] = 16 << 20

#: How much of each candidate the cheap pre-filter reads (head, middle, tail).
SLICE: Final[int] = 8 << 20

#: Pseudo-filesystems a scan must not descend into: bottomless (/proc) or
#: meaningless to hash. Everything else — /nix, /var, /tmp — is an ordinary tree
#: and *is* scanned when the root contains it; silently skipping /nix/store would
#: hide exactly the duplicates worth finding.
SKIP_DIRS: Final[tuple[str, ...]] = ("/proc", "/sys", "/dev", "/run")

#: Where progress lines go (the CLI prints them to stderr).
_Log = Callable[[str], None]

#: Names the fd binary ships under: Debian and Ubuntu call it `fdfind`.
FD_NAMES: Final[tuple[str, ...]] = ("fd", "fdfind")


def find_fd() -> str | None:
    """Path to the fd binary, under either of the names distributions use.

    Debian/Ubuntu package it as `fd-find` and install `fdfind`, because `fd`
    collides with an unrelated tool already in their archive. Assuming the name
    means silently losing the fast path on those systems — which is precisely the
    kind of failure that looks like success.
    """
    for name in FD_NAMES:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def _noop(_message: str) -> None:
    """Default progress sink: stay quiet."""


def human(n: int | float) -> str:
    """Format a byte count with decimal units, matching what the FS reports."""
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < 1000:
            return f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{value:.1f} PB"


_SUFFIXES: Final[dict[str, int]] = {
    "": 1,
    "B": 1,
    "K": 1000,
    "KB": 1000,
    "M": 1000**2,
    "MB": 1000**2,
    "G": 1000**3,
    "GB": 1000**3,
    "T": 1000**4,
    "TB": 1000**4,
    "KI": 1024,
    "KIB": 1024,
    "MI": 1024**2,
    "MIB": 1024**2,
    "GI": 1024**3,
    "GIB": 1024**3,
    "TI": 1024**4,
    "TIB": 1024**4,
}


def parse_size(text: str) -> int:
    """Parse ``100M``, ``2 GiB`` or ``1,5G`` into a byte count.

    Accepts the French/English ``o``/``B`` byte suffix and a comma as the decimal
    separator, because both show up in the wild on this tool's home turf.
    """
    cleaned = "".join(text.split()).upper().replace(",", ".")
    cut = len(cleaned)
    while cut and cleaned[cut - 1].isalpha():
        cut -= 1
    number, suffix = cleaned[:cut], cleaned[cut:]
    try:
        value = float(number)
    except ValueError as error:
        raise ValueError(f"not a size: {text!r}") from error
    # 'o' is the French for 'octet'; accept both spellings everywhere.
    unit = suffix.replace("O", "B")
    if unit not in _SUFFIXES:
        raise ValueError(f"unknown size suffix in {text!r}")
    return int(value * _SUFFIXES[unit])


# --------------------------------------------------------------------------- #
# candidate discovery
# --------------------------------------------------------------------------- #


def _is_within(path: str, parent: str) -> bool:
    """Is ``path`` the directory ``parent``, or below it?"""
    parent = parent.rstrip("/") or "/"
    if parent == "/":
        return path.startswith("/")
    return path == parent or path.startswith(parent + "/")


def skipped_under(root: str) -> tuple[str, ...]:
    """Which pseudo-filesystems a scan of ``root`` has to stay out of.

    The ones *below* the root — those are the only ones a walk could reach. A
    root that lives inside one of them is never skipped: scanning ``/proc/self``
    is odd but it is what was asked for, and scanning ``/home/x`` must not be
    slowed down by a rule about ``/proc``.
    """
    absolute = os.path.abspath(root).rstrip("/") or "/"
    return tuple(s for s in SKIP_DIRS if s != absolute and _is_within(s, absolute))


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A regular file large enough to be worth deduplicating."""

    path: str
    size: int
    inode: int


@dataclass(slots=True)
class Group:
    """Files that share a cheap signature, and therefore *may* be identical."""

    size: int
    paths: list[str]
    inodes: list[int]

    @property
    def waste(self) -> int:
        """Bytes recoverable if every member but one turns out identical."""
        return self.size * (len(self.paths) - 1)

    def to_json(self) -> dict[str, Any]:
        return {"size": self.size, "paths": self.paths, "inos": self.inodes}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Group:
        return cls(
            size=int(raw["size"]),
            paths=[str(p) for p in raw["paths"]],
            inodes=[int(i) for i in raw["inos"]],
        )


def walk(root: str, min_size: int) -> list[FileEntry]:
    """Walk ``root`` with the standard library. Correct but slow: ~119 s for a
    4 TB home, against ~4 s for :func:`walk_fd` on the same tree."""
    root = os.path.abspath(root)
    banned = skipped_under(root)
    found: list[FileEntry] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        if any(_is_within(directory, b) for b in banned):
            continue
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        if stat.st_size >= min_size:
                            found.append(FileEntry(entry.path, stat.st_size, stat.st_ino))
                except OSError:
                    continue
    return found


def walk_fd(root: str, min_size: int) -> list[FileEntry] | None:
    """Same result as :func:`walk`, but ~25x faster: ``fd`` enumerates and
    filters in parallel, ``stat`` fills in the size and the inode.

    ``--no-ignore`` and ``--hidden`` matter: without them fd honours
    ``.gitignore`` and silently skips most build trees — including the
    ``.git/objects/pack`` files that are regularly the biggest duplicates on a
    real machine. Pseudo-filesystems are filtered out afterwards, with the same
    predicate :func:`walk` uses, so the two functions agree by construction.
    Returns ``None`` when ``fd`` is absent.

    ``-X`` batches many paths per ``stat`` invocation instead of ``-x``'s one
    process per file. Measured on 29 838 files: 4.5 s against 13.9 s, for byte
    identical output. It only matters below ~100 MB, where the file count
    explodes, but that is exactly where a deep scan lives.
    """
    binary = find_fd()
    if binary is None:
        return None
    command = [
        binary,
        "--no-ignore",
        "--hidden",
        "-t",
        "f",
        "-S",
        f"+{min_size}B",
        ".",
        os.path.abspath(root),
        "-X",
        "stat",
        "-c",
        "%s|%i|%n",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    banned = skipped_under(root)
    found: list[FileEntry] = []
    for line in completed.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            size, inode = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if any(_is_within(parts[2], b) for b in banned):
            continue
        found.append(FileEntry(parts[2], size, inode))
    return found


def load_index(path: str) -> list[FileEntry]:
    """Read a ``size|inode|path`` list, as produced by ``fd … -x stat``."""
    found: list[FileEntry] = []
    with open(path, errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("|", 2)
            if len(parts) != 3:
                continue
            try:
                found.append(FileEntry(parts[2], int(parts[0]), int(parts[1])))
            except ValueError:
                continue
    return found


def signature(path: str, size: int) -> str | None:
    """A cheap fingerprint: the size plus 8 MiB each from head, middle and tail.

    Deliberately not a full-content hash. It only has to avoid wasting ioctls on
    pairs that obviously differ; the kernel decides the real question later.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())
    try:
        with open(path, "rb", buffering=0) as handle:
            for offset in (0, max(0, size // 2 - SLICE // 2), max(0, size - SLICE)):
                handle.seek(offset)
                digest.update(handle.read(SLICE))
    except OSError:
        return None
    return digest.hexdigest()


@dataclass(slots=True)
class ScanResult:
    """What :func:`scan` found, plus the timings worth reporting."""

    root: str
    min_size: int
    entries: int
    candidates: int
    groups: list[Group]
    elapsed: float

    @property
    def waste(self) -> int:
        return sum(group.waste for group in self.groups)

    def to_json(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "min_size": self.min_size,
            "groups": [group.to_json() for group in self.groups],
        }


def scan(
    root: str,
    min_size: int = 100_000_000,
    *,
    jobs: int = 8,
    walk_mode: str = "auto",
    index: str | None = None,
    log: _Log = _noop,
) -> ScanResult:
    """Find candidate duplicate groups under ``root``. Read-only."""
    started = time.monotonic()
    log(f"scanning {root} (>= {human(min_size)})")

    entries: list[FileEntry] | None = None
    if index is not None:
        log(f"reading index {index}")
        entries = load_index(index)
    elif walk_mode in ("auto", "fd"):
        banned = skipped_under(root)
        if banned and walk_mode == "auto":
            log(f"walking with Python: fd would descend into {', '.join(banned)}")
        else:
            entries = walk_fd(root, min_size)
            if entries is None and walk_mode == "fd":
                raise RuntimeError(
                    "fd was requested with --walk fd but neither fd nor fdfind is on PATH"
                )
    if entries is None:
        entries = walk(root, min_size)
    log(f"{len(entries)} files >= {human(min_size)} in {time.monotonic() - started:.1f}s")

    by_size: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in entries:
        by_size[entry.size].append(entry)
    candidates = [e for same in by_size.values() if len(same) > 1 for e in same]
    log(f"{len(candidates)} share a size with another file; fingerprinting them")

    digests: dict[str, list[FileEntry]] = defaultdict(list)
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            (pool.submit(signature, entry.path, entry.size), entry) for entry in candidates
        ]
        for future, entry in futures:
            digest = future.result()
            done += 1
            if done % 250 == 0:
                log(f"  fingerprinted {done}/{len(candidates)}")
            if digest is not None:
                digests[digest].append(entry)

    groups: list[Group] = []
    for same in digests.values():
        if len(same) < 2:
            continue
        if len({e.inode for e in same}) < 2:
            # Hardlinks: the same inode twice. Nothing to recover.
            continue
        same.sort(key=lambda e: e.path)
        groups.append(
            Group(
                size=same[0].size,
                paths=[e.path for e in same],
                inodes=[e.inode for e in same],
            )
        )
    groups.sort(key=lambda g: -g.waste)

    elapsed = time.monotonic() - started
    result = ScanResult(
        root=os.path.abspath(root),
        min_size=min_size,
        entries=len(entries),
        candidates=len(candidates),
        groups=groups,
        elapsed=elapsed,
    )
    log(
        f"{len(groups)} duplicate groups, {human(result.waste)} at stake, in {elapsed:.1f}s"
    )
    return result


def write_groups(result: ScanResult, path: str) -> None:
    with open(path, "w") as handle:
        json.dump(result.to_json(), handle, indent=1)


def read_groups(path: str) -> tuple[str, int, list[Group]]:
    with open(path) as handle:
        raw: dict[str, Any] = json.load(handle)
    return (
        str(raw.get("root", "")),
        int(raw.get("min_size", 0)),
        [Group.from_json(g) for g in raw["groups"]],
    )


# --------------------------------------------------------------------------- #
# the dedupe itself
# --------------------------------------------------------------------------- #


def dedupe_range(source_fd: int, dest_fd: int, offset: int, length: int) -> int:
    """Ask the kernel to share one range. Returns the bytes it *claims* to share.

    Zero means "these differ" (status ``FILE_DEDUPE_RANGE_DIFFERS``) or "the FS
    refused" (negative status); either way nothing has been modified.

    Careful: on bcachefs 1.39.5 the answer is an **upper bound**, not a
    measurement. Two 32 MiB files differing by 4 KiB at 10 MiB, deduplicated over
    a 16 MiB request, are reported as ``deduped=16777216, status=0`` — the kernel
    shares the parts that match and reports the whole range. Byte-for-byte
    comparison of both files before and after shows the content untouched, so the
    number lies, not the operation. Callers that need a real figure have to ask
    the filesystem how much space it actually holds (:func:`filesystem_used`).
    """
    info = struct.pack("<qQQiI", dest_fd, offset, 0, 0, 0)
    argument = struct.pack("<QQHHI", offset, length, 1, 0, 0) + info
    buffer = ctypes.create_string_buffer(argument, len(argument))
    fcntl.ioctl(source_fd, FIDEDUPERANGE, buffer)
    reply = struct.unpack("<qQQiI", bytes(buffer)[24:])
    deduped, status = int(reply[2]), int(reply[3])
    if status != 0:
        return 0
    return deduped


def dedupe_pair(master: str, other: str, size: int) -> int:
    """Share ``other``'s extents with ``master``'s. Returns the bytes claimed.

    The walk is chunk by chunk and always advances by the chunk length, never by
    what the kernel answered. Advancing by the answer would be wrong given the
    over-reporting above: a chunk reported as fully shared but only partly shared
    would silently skip the rest of the file.
    """
    total = 0
    with (
        open(master, "rb", buffering=0) as source,
        open(other, "rb", buffering=0) as dest,
    ):
        offset = 0
        while offset < size:
            length = min(DEDUPE_CHUNK, size - offset)
            total += dedupe_range(source.fileno(), dest.fileno(), offset, length)
            offset += length
    # Never claim more than the file holds, whatever the kernel replied.
    return min(total, size)


@dataclass(slots=True)
class ApplyResult:
    """What :func:`apply_groups` achieved.

    ``gained`` is what the kernel claimed, so it is an **upper bound** — see
    :func:`dedupe_range`. ``measured`` is the filesystem's own accounting delta
    when it could be read, and is the number to believe; it is ``None`` when no
    ``bcachefs`` binary was available.
    """

    gained: int
    deduped: int
    groups: int
    elapsed: float
    measured: int | None = None

    @property
    def rate(self) -> float:
        return self.gained / self.elapsed if self.elapsed else 0.0


def filesystem_used(mountpoint: str = "/") -> int | None:
    """Space bcachefs says it holds, deduplicated — the only honest measurement.

    Returns ``None`` when the filesystem is not bcachefs or the tool is missing.
    """
    if shutil.which("bcachefs") is None:
        return None
    completed = subprocess.run(
        ["bcachefs", "fs", "usage", "-f", "replicas", mountpoint],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("1x:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1])
    return None


def process_group(group: Group, dry_run: bool) -> tuple[int, int]:
    """Reduce one group to a set of shared extents.

    The kernel is the oracle, so a group whose members merely *look* alike simply
    costs one refused ioctl per member. When a member differs it becomes the next
    master, so a group of N distinct contents costs O(N) comparisons, not O(N^2).
    """
    paths = list(group.paths)
    if len(paths) < 2:
        return 0, 0
    if dry_run:
        # Estimate only: same signature, so assume the best case.
        return group.waste, len(paths) - 1

    gained = 0
    deduped = 0
    index = 0
    while index < len(paths) - 1:
        master = paths[index]
        remaining: list[str] = []
        for other in paths[index + 1 :]:
            shared = dedupe_pair(master, other, group.size)
            if shared:
                gained += shared
                deduped += 1
            else:
                remaining.append(other)
        if not remaining:
            break
        paths = [master, *remaining]
        index += 1
    return gained, deduped


def apply_groups(
    groups: Sequence[Group],
    *,
    jobs: int = 8,
    dry_run: bool = False,
    log: _Log = _noop,
) -> ApplyResult:
    """Deduplicate every group concurrently. Never deletes anything."""
    started = time.monotonic()
    before = None if dry_run else filesystem_used()
    gained = 0
    deduped = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = pool.map(lambda group: process_group(group, dry_run), groups)
        for number, (group_gained, group_deduped) in enumerate(results, 1):
            gained += group_gained
            deduped += group_deduped
            if number % 25 == 0 or number == len(groups):
                elapsed = time.monotonic() - started
                log(
                    f"  {number}/{len(groups)} groups, {deduped} files, "
                    f"{human(gained)}, {elapsed:.1f}s"
                )
    measured = None
    if before is not None:
        os.sync()
        after = filesystem_used()
        if after is not None:
            measured = before - after
    return ApplyResult(
        gained=gained,
        deduped=deduped,
        groups=len(groups),
        elapsed=time.monotonic() - started,
        measured=measured,
    )
