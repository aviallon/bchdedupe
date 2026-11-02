"""Tests for bchdedupe.

The interesting tests need a filesystem that implements ``FIDEDUPERANGE``
(bcachefs, btrfs, or XFS with reflink). They skip when the filesystem refuses, so
the suite stays honest on an ext4 CI runner instead of failing there.

``/tmp`` is a tmpfs on plenty of machines, including the one this was written on,
and a tmpfs cannot deduplicate. So the tests look for a capable directory — honour
``BCHDEDUPE_TEST_DIR`` if you have a better one — instead of assuming the temp dir
will do.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bchdedupe import cli, core

if TYPE_CHECKING:
    from collections.abc import Iterator

BLOCK = 64 << 10
_UNIT = b"bchdedupe-test-payload-"
#: Big enough that the cheap prefilter does not read the whole file: three 8 MiB
#: slices out of 4 MiB (or less) cover everything, so the subtle tests need more.
PAYLOAD = (_UNIT * ((4 << 20) // len(_UNIT) + 1))[: 4 << 20]
BIG_PAYLOAD = (_UNIT * ((32 << 20) // len(_UNIT) + 1))[: 32 << 20]


def write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def same_size_but_different(size: int, seed: int) -> bytes:
    out = bytearray()
    counter = seed
    while len(out) < size:
        out += counter.to_bytes(8, "little")
        counter += 1
    return bytes(out[:size])


def dedupe_supported(directory: Path) -> bool:
    """Is ``FIDEDUPERANGE`` implemented *and* effective on this filesystem?"""
    try:
        probe = write(directory / ".bchdedupe-probe-a", os.urandom(BLOCK))
        other = write(directory / ".bchdedupe-probe-b", probe.read_bytes())
    except OSError:
        return False
    try:
        with probe.open("rb") as source, other.open("rb") as dest:
            shared = core.dedupe_range(source.fileno(), dest.fileno(), 0, BLOCK)
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)
        other.unlink(missing_ok=True)
    return shared == BLOCK


def _candidate_bases() -> list[Path]:
    bases = []
    if preferred := os.environ.get("BCHDEDUPE_TEST_DIR"):
        bases.append(Path(preferred))
    bases.append(Path(tempfile.gettempdir()))
    bases.append(Path.home() / ".cache")
    return bases


def _pick_workspace_base() -> Path | None:
    for base in _candidate_bases():
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if dedupe_supported(base):
            return base
    return None


def filesystem_type(path: Path) -> str:
    """The filesystem type a path lives on, from /proc/mounts (longest match)."""
    resolved = str(path.resolve())
    best, kind = "", "unknown"
    with open("/proc/mounts", errors="replace") as mounts:
        for line in mounts:
            parts = line.split()
            if len(parts) < 3:
                continue
            mountpoint = parts[1].replace("\\040", " ")
            if resolved.startswith(mountpoint) and len(mountpoint) >= len(best):
                best, kind = mountpoint, parts[2]
    return kind


WORKSPACE_BASE = _pick_workspace_base()

needs_dedupe = pytest.mark.skipif(
    WORKSPACE_BASE is None,
    reason=(
        "no filesystem that implements FIDEDUPERANGE (bcachefs/btrfs/xfs do): tried "
        + ", ".join(str(b) for b in _candidate_bases())
        + " — set BCHDEDUPE_TEST_DIR to point at one"
    ),
)


@pytest.fixture
def workdir() -> Iterator[Path]:
    """A scratch directory on a filesystem that can actually deduplicate."""
    base = WORKSPACE_BASE or Path(tempfile.gettempdir())
    directory = Path(tempfile.mkdtemp(prefix="bchdedupe-test-", dir=base))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


def test_parse_size_accepts_human_suffixes() -> None:
    assert core.parse_size("100M") == 100_000_000
    assert core.parse_size("1,5G") == 1_500_000_000
    assert core.parse_size("2 GiB") == 2 * 1024**3
    assert core.parse_size("512K") == 512_000
    assert core.parse_size("4096") == 4096
    assert core.parse_size("3To") == 3 * 1000**4


def test_parse_size_rejects_nonsense() -> None:
    for bad in ("", "M", "12 furlongs", "1.2.3G"):
        with pytest.raises(ValueError):
            core.parse_size(bad)


def test_human_uses_decimal_units() -> None:
    assert core.human(0) == "0.0 B"
    assert core.human(1500) == "1.5 kB"
    assert core.human(177_300_000_000) == "177.3 GB"


def test_group_waste_is_per_extra_member() -> None:
    group = core.Group(size=1000, paths=["a", "b", "c"], inodes=[1, 2, 3])
    assert group.waste == 2000


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_fd_binary_is_found_under_either_distribution_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debian ships fd as `fdfind`; assuming the name silently loses the fast path."""
    seen: list[str] = []

    def only(names: str) -> str | None:
        seen.append(names)
        return "/usr/bin/fdfind" if names == "fdfind" else None

    monkeypatch.setattr(shutil, "which", only)
    assert core.find_fd() == "/usr/bin/fdfind"
    assert seen == ["fd", "fdfind"]

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert core.find_fd() is None


def test_scan_groups_identical_files_and_ignores_lookalikes(workdir: Path) -> None:
    write(workdir / "a/one.bin", PAYLOAD)
    write(workdir / "b/two.bin", PAYLOAD)
    write(workdir / "c/three.bin", PAYLOAD)
    # Same size, different content: must not end up in the group.
    write(workdir / "d/odd.bin", same_size_but_different(len(PAYLOAD), seed=1))

    result = core.scan(str(workdir), 1, walk_mode="python")

    assert len(result.groups) == 1
    assert len(result.groups[0].paths) == 3
    assert result.waste == 2 * len(PAYLOAD)


def test_scan_ignores_hardlinks(workdir: Path) -> None:
    """Two names for one inode look like duplicates and are worth nothing."""
    original = write(workdir / "a/original.bin", PAYLOAD)
    (workdir / "b").mkdir()
    os.link(original, workdir / "b/linked.bin")

    assert core.scan(str(workdir), 1, walk_mode="python").groups == []


def test_scan_respects_min_size(workdir: Path) -> None:
    write(workdir / "a/small.bin", PAYLOAD[:BLOCK])
    write(workdir / "b/small.bin", PAYLOAD[:BLOCK])

    assert core.scan(str(workdir), 1 << 20, walk_mode="python").groups == []
    assert len(core.scan(str(workdir), 1, walk_mode="python").groups) == 1


def test_skipped_under_only_reports_pseudo_filesystems_below_the_root() -> None:
    """The do-not-enter list must be relative to the root, not absolute.

    Getting this backwards is invisible in the results and merely disables the
    fast indexer everywhere, which is exactly the kind of bug that survives a
    green test suite for months.
    """
    assert core.skipped_under("/home/someone") == ()
    assert core.skipped_under("/tmp/scratch") == ()
    assert set(core.skipped_under("/")) == set(core.SKIP_DIRS)
    # A root inside a skipped directory is not skipped itself.
    assert core.skipped_under("/proc") == ()
    assert core.skipped_under("/proc/self") == ()
    assert core.skipped_under("/run") == ()


def test_scan_finds_duplicates_under_a_root_that_is_a_skipped_path(workdir: Path) -> None:
    scratch = workdir / "var"
    write(scratch / "one.bin", PAYLOAD)
    write(scratch / "two.bin", PAYLOAD)

    assert len(core.scan(str(scratch), 1, walk_mode="python").groups) == 1


@pytest.mark.skipif(core.find_fd() is None, reason="fd/fdfind is not installed")
def test_fd_index_finds_exactly_what_the_python_walk_finds(workdir: Path) -> None:
    """The optimisation must not change which files are considered.

    The ``.git`` case is not decoration: the biggest duplicate group on a real
    machine was a 4 GB pack file sitting in three ``.git/objects/pack``
    directories, so an indexer that skips VCS internals would miss exactly the
    files this tool exists to find.
    """
    write(workdir / "a/one.bin", PAYLOAD)
    write(workdir / "b/two.bin", PAYLOAD)
    write(workdir / "build/vendor/nested/deep.bin", PAYLOAD)
    write(workdir / ".git/objects/pack/pack-0000.pack", PAYLOAD)
    write(workdir / "c/tiny.bin", b"x")

    by_python = {entry.path for entry in core.walk(str(workdir), 1)}
    by_fd = core.walk_fd(str(workdir), 1)

    assert any(".git/objects" in path for path in by_python)
    assert by_fd is not None
    assert {entry.path for entry in by_fd} == by_python
    from_index = {entry.path for entry in core.load_index(_index_file(workdir))}
    assert from_index == by_python


def _index_file(directory: Path) -> str:
    index = directory / "index.txt"
    binary = core.find_fd()
    assert binary is not None
    completed = subprocess.run(
        [
            binary,
            "--no-ignore",
            "--hidden",
            "-t",
            "f",
            "-S",
            "+1B",
            ".",
            str(directory),
            "-x",
            "stat",
            "-c",
            "%s|%i|%n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    index.write_text(completed.stdout)
    return str(index)


# --------------------------------------------------------------------------- #
# the dedupe itself
# --------------------------------------------------------------------------- #


@needs_dedupe
def test_dedupe_shares_extents_for_identical_files(workdir: Path) -> None:
    master = write(workdir / "master.bin", PAYLOAD)
    other = write(workdir / "other.bin", PAYLOAD)

    assert core.dedupe_pair(str(master), str(other), len(PAYLOAD)) == len(PAYLOAD)
    # Whatever happened to the extents, both files must still read back exactly.
    assert master.read_bytes() == PAYLOAD
    assert other.read_bytes() == PAYLOAD


@needs_dedupe
def test_dedupe_refuses_same_size_different_content(workdir: Path) -> None:
    """Nothing at all to share: every chunk must come back empty-handed."""
    one = write(workdir / "one.bin", same_size_but_different(len(PAYLOAD), seed=1))
    two = write(workdir / "two.bin", same_size_but_different(len(PAYLOAD), seed=2))

    assert core.dedupe_pair(str(one), str(two), len(PAYLOAD)) == 0
    assert one.read_bytes() != two.read_bytes()


@needs_dedupe
def test_a_differing_block_is_never_shared_though_the_kernel_over_reports(
    workdir: Path,
) -> None:
    """The safety property, and a bcachefs answer that lies about its extent.

    The signature reads three 8 MiB slices, so in a 32 MiB file they cover
    [0,8), [12,20) and [24,32): a change around 10 MiB is invisible to it and the
    two files land in the same candidate group on purpose.

    Verified on bcachefs 1.39.5: the kernel answers ``deduped=16777216,
    status=0`` here, having shared only the parts that match. What must never
    happen is the destination losing its bytes, so *that* is what is asserted —
    the returned number deliberately is not, and the tool reports it as an upper
    bound (see :func:`core.dedupe_range`).
    """
    content = bytearray(BIG_PAYLOAD)
    content[10 << 20 : (10 << 20) + 4096] = b"\xff" * 4096
    expected = bytes(content)
    one = write(workdir / "one.bin", BIG_PAYLOAD)
    two = write(workdir / "two.bin", expected)
    from_before = {str(one): one.read_bytes(), str(two): two.read_bytes()}

    # The premise of the test, asserted rather than assumed.
    assert core.signature(str(one), len(BIG_PAYLOAD)) == core.signature(
        str(two), len(BIG_PAYLOAD)
    )
    assert one.read_bytes() != two.read_bytes()

    claimed = core.dedupe_pair(str(one), str(two), len(BIG_PAYLOAD))

    for path in (one, two):
        assert path.read_bytes() == from_before[str(path)]
    assert two.read_bytes()[10 << 20 : (10 << 20) + 4096] == b"\xff" * 4096
    assert 0 <= claimed <= len(BIG_PAYLOAD)


@needs_dedupe
def test_apply_dedupes_the_real_duplicates_only(workdir: Path) -> None:
    write(workdir / "a/one.bin", PAYLOAD)
    write(workdir / "b/two.bin", PAYLOAD)
    write(workdir / "c/three.bin", PAYLOAD)
    odd = write(workdir / "d/odd.bin", same_size_but_different(len(PAYLOAD), seed=3))
    odd_before = odd.read_bytes()

    result = core.scan(str(workdir), 1, walk_mode="python")
    applied = core.apply_groups(result.groups)

    assert applied.deduped == 2
    assert applied.gained == 2 * len(PAYLOAD)
    assert odd.read_bytes() == odd_before


def test_apply_dry_run_touches_nothing(workdir: Path) -> None:
    write(workdir / "a/one.bin", PAYLOAD)
    write(workdir / "b/two.bin", PAYLOAD)
    before = (workdir / "a/one.bin").stat().st_mtime_ns

    result = core.scan(str(workdir), 1, walk_mode="python")
    estimate = core.apply_groups(result.groups, dry_run=True)

    assert estimate.gained == len(PAYLOAD)
    assert (workdir / "a/one.bin").stat().st_mtime_ns == before


# --------------------------------------------------------------------------- #
# the algorithm, on filesystems that cannot deduplicate at all
# --------------------------------------------------------------------------- #
#
# These run everywhere, including the ext4 runner in CI, and they are the only
# part of the safety argument CI can actually check. They pin the two things that
# would be silent if wrong: that every byte of a file is requested exactly once,
# and that a refusal does not abort the rest of the group.


def _sparse(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def test_dedupe_pair_covers_the_file_chunk_by_chunk(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    """Every byte asked for exactly once — the walk must not follow the reply.

    Advancing by what the kernel answered would be wrong on bcachefs: it can
    report a whole chunk after sharing only part of it, and the rest of the file
    would then never be looked at.
    """
    size = 3 * core.DEDUPE_CHUNK + 5
    one = _sparse(workdir / "one.bin", size)
    two = _sparse(workdir / "two.bin", size)
    asked: list[tuple[int, int]] = []

    def refuse(_source: int, _dest: int, offset: int, length: int) -> int:
        asked.append((offset, length))
        return 0

    monkeypatch.setattr(core, "dedupe_range", refuse)

    assert core.dedupe_pair(str(one), str(two), size) == 0
    assert asked == [
        (0, core.DEDUPE_CHUNK),
        (core.DEDUPE_CHUNK, core.DEDUPE_CHUNK),
        (2 * core.DEDUPE_CHUNK, core.DEDUPE_CHUNK),
        (3 * core.DEDUPE_CHUNK, 5),
    ]


def test_dedupe_pair_never_claims_more_than_the_file_holds(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    """An over-reporting kernel must not inflate the reported gain."""
    size = 2 * core.DEDUPE_CHUNK
    one = _sparse(workdir / "one.bin", size)
    two = _sparse(workdir / "two.bin", size)
    monkeypatch.setattr(core, "dedupe_range", lambda _s, _d, _o, length: length * 4)

    assert core.dedupe_pair(str(one), str(two), size) == size


def test_process_group_keeps_going_after_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member that differs becomes the next master; it is not counted."""
    group = core.Group(size=1000, paths=["/a1", "/a2", "/b1", "/a3"], inodes=[1, 2, 3, 4])

    def fake_pair(master: str, other: str, _size: int) -> int:
        return 1000 if master[:2] == other[:2] else 0

    monkeypatch.setattr(core, "dedupe_pair", fake_pair)

    assert core.process_group(group, dry_run=False) == (2000, 2)


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #


def test_cli_scan_then_dry_run_apply(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = workdir / "groups.json"
    write(workdir / "data/a.bin", PAYLOAD)
    write(workdir / "data/b.bin", PAYLOAD)
    data = str(workdir / "data")

    assert cli.main(["scan", data, "--min-size", "1", "--out", str(groups)]) == 0
    _root, _min_size, parsed = core.read_groups(str(groups))
    assert len(parsed) == 1

    assert cli.main(["apply", str(groups), "--dry-run"]) == 0
    assert "would be deduplicated" in capsys.readouterr().err


@needs_dedupe
def test_cli_apply_reports_what_it_reclaimed(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = workdir / "groups.json"
    write(workdir / "data/a.bin", PAYLOAD)
    write(workdir / "data/b.bin", PAYLOAD)
    data = str(workdir / "data")
    assert cli.main(["scan", data, "--min-size", "1", "--out", str(groups)]) == 0

    assert cli.main(["apply", str(groups)]) == 0

    captured = capsys.readouterr()
    assert "1 files deduplicated" in captured.err
    assert "4.2 MB reclaimed" in captured.err


# --------------------------------------------------------------------------- #
# integration
# --------------------------------------------------------------------------- #


def test_filesystem_used_reads_bcachefs_accounting() -> None:
    """Pin the parser against the real command, not a fixture of my own making."""
    if shutil.which("bcachefs") is None:
        pytest.skip("not a bcachefs system")
    assert (core.filesystem_used("/") or 0) > 0
    assert core.filesystem_used("/definitely/not/a/mountpoint") is None


# There is deliberately no automated "did bcachefs free the space" test.
#
# `bcachefs fs usage` is the only figure worth reading on bcachefs, and it answers
# far too lazily to be an oracle: measured on this machine, deduplicating a 128 MiB
# pair moved it by *nothing* in 180 seconds of polling (it drifted up by 9 MB from
# unrelated churn instead), while a 17-minute real run moved it by 83 GB. A test
# built on that either fails at random or always skips, and neither is worth
# keeping. The end-to-end proof lives in docs/why-not-duperemove.md, with the
# numbers from that real run.


@pytest.mark.integration
@needs_dedupe
@pytest.mark.skipif(WORKSPACE_BASE is None, reason="no dedupe-capable filesystem")
def test_integration_statvfs_shows_the_space_coming_back(workdir: Path) -> None:
    """Free space must move, measured the ordinary way.

    This is the test CI can run: a loop-backed btrfs reports its free space
    accurately, so `statvfs` before and after is a real measurement, and it needs
    no bcachefs binary.

    It is skipped on bcachefs on purpose. Measured there: deduplicating a 16.8 MB
    pair leaves `statvfs` free space 1 MB higher, not 16 — bcachefs's accounting
    lags, and `bcachefs fs usage` is the only figure worth reading on it (and it is
    too lazy to serve as a test oracle; see the note further up).
    """
    assert WORKSPACE_BASE is not None
    if filesystem_type(WORKSPACE_BASE) == "bcachefs":
        pytest.skip("bcachefs's statvfs lags; see the note above")

    size = 32 << 20
    payload = (_UNIT * (size // len(_UNIT) + 1))[:size]
    write(workdir / "a.bin", payload)
    write(workdir / "b.bin", payload)
    os.sync()
    before = shutil.disk_usage(workdir).free

    result = core.scan(str(workdir), 1, walk_mode="python")
    applied = core.apply_groups(result.groups)
    os.sync()
    after = shutil.disk_usage(workdir).free
    for _ in range(30):
        if after - before >= size // 2:
            break
        time.sleep(1)
        os.sync()
        after = shutil.disk_usage(workdir).free

    assert applied.gained == size
    assert after - before >= size // 2, (
        f"free space went {before} -> {after} for a {size} byte duplicate"
    )
