# bchdedupe

Deduplicate files on **bcachefs** without hashing anything. `FIDEDUPERANGE` makes
the filesystem compare the extents itself, so a 4 TB home is scanned in seconds,
the dedupe phase is metadata-only, and **no file is ever deleted or rewritten**.

## Why this exists

`duperemove` is the usual answer, and it does not work here: it needs `FIEMAP` to
find the extents to hash, and bcachefs [does not implement
it](docs/why-not-duperemove.md). What duperemove falls back to — reading every
file in 128 KiB blocks and hashing it — turns a 23-second job into an overnight
one, on a filesystem that was never going to need those hashes in the first
place.

## Provenance

**This tool was written by an AI.** The code comes from a
`deepseek/deepseek-v4.1-flash` agent driven by the [`pi` coding
agent](https://github.com/badlogic/pi-mono), during a session spent cleaning up a
real, 95 %-full 4 TB bcachefs filesystem. The problem, the direction and the
review are the human's (Antoine Viallon); the code is the model's. Commits carry a
`Co-Authored-By:` trailer naming the model.

Treat it accordingly: read it before pointing it at data you care about, and
expect the failure modes of a generated program. What it does is easy to audit —
it calls exactly one ioctl — and it prints the filesystem's *own* accounting next
to its own claim, so you can see for yourself whether it did what it says. The
test suite was written to disagree with the model: one of its tests caught
bcachefs over-reporting how much it had deduplicated (see
[docs/why-not-duperemove.md](docs/why-not-duperemove.md#one-caveat-verified)).

## Requirements

* Linux, Python ≥ 3.11
* a filesystem that implements `FIDEDUPERANGE`: **bcachefs** (btrfs and XFS with
  reflink also work, but there duperemove is already fine)
* `fd` — optional, strongly recommended: it indexes candidates ~25× faster than
  the built-in walk (4 s vs 119 s on a 4 TB home). Debian and Ubuntu ship it as
  `fdfind`, which is looked for too
* `bcachefs` (from `bcachefs-tools`) — optional: used only to report how much
  space the filesystem says it actually freed

## Install

```sh
# from a checkout, with Nix
nix run github:aviallon/bchdedupe -- scan /home

# or with pip
pip install git+https://github.com/aviallon/bchdedupe
```

## Usage

```sh
# 1. find candidate duplicates (read-only; writes a group list)
bchdedupe scan /home --min-size 100M --out groups.json

# 2. see what it would do
bchdedupe apply groups.json --dry-run

# 3. do it
bchdedupe apply groups.json --jobs 8
```

`scan` only reads: it walks the tree, groups files by size, and fingerprints the
groups with three 8 MiB slices (head, middle, tail). `apply` then walks each
group and asks the kernel to share extents. The kernel refuses when the contents
differ, so a group that merely *looked* alike costs one pointless ioctl and
nothing else.

Sensible options:

| Option | Why you would set it |
|---|---|
| `--min-size 10M` | the tail of small files adds up; 100M is the fast default |
| `--walk python` | if you do not trust `fd`'s notion of what to skip |
| `--index FILE` | reuse a `fd … -x stat` listing instead of walking again |
| `--jobs N` | the dedupe is kernel-bound, so more threads rarely help |

## Reading the output

```
251 groups, 177.3 GB at stake, jobs=8, dry_run=False
  50/251 groups, 252 files, 117.5 GB, 710.8s

545 files deduplicated, 177.3 GB reclaimed in 900.2s (197.0 MB/s)
filesystem accounting says 171.4 GB (upper bound above is what the kernel claimed)
```

The second number is the one to believe. The kernel's `bytes_deduped` is an upper
bound: it can report a whole requested range after sharing only part of it, which
is why the tool asks `bcachefs fs usage` before and after and prints the delta.
Filesystem accounting lags behind the operation, so the delta is a floor.

## What it does not do

* **It never deletes anything**, and never rewrites content. Deduplication only
  makes extents shared.
* **It does not deduplicate inodes**: two names for one inode are reported as
  worth nothing, because they are.
* **It does not cross filesystems.** One grouping per run, one filesystem.
* **It does not hash.** That is the point, and also the limit: the safety
  argument rests on the kernel's extent comparison, not on a digest bchdedupe
  computed. If you want proof a pair is identical before sharing, this tool is
  not that tool.

## Documentation

* [`docs/why-not-duperemove.md`](docs/why-not-duperemove.md) — the `FIEMAP`
  measurement, the alternatives that do not help, and the over-report caveat
* [`AGENTS.md`](AGENTS.md) — how commits and documentation are expected to look
  here
* [`hooks/README.md`](hooks/README.md) — the git hooks, and what CI cannot cover

## Licence

MIT, see [LICENSE](LICENSE).
