# Why duperemove is the wrong tool on bcachefs

This is the observation the whole project grew out of, written down so nobody
has to rediscover it.

## The symptom

Deduplicating a 4 TB bcachefs home with `duperemove -r -d` looked like it would
take hours, with the disk nearly saturated on *reads it should not need*.

## The cause: no `FIEMAP`

`duperemove` does not hash a file blindly. It asks the filesystem where the
file's extents are, hashes one block per extent, and can then deduplicate
extent by extent. That map comes from `FIEMAP`. On bcachefs:

```
$ filefrag -v frag.bin
Filesystem type is: ca451a4e
File size of frag.bin is 8388608 (2048 blocks of 4096 bytes)
 ext:     logical_offset:        physical_offset: length:   expected: flags:
   0:        0..    2047:          0..         0:      0: last,unknown_loc,delalloc,eof
frag.bin: 1 extent found
```

`mapped_extents = 0`, all flags `unknown_loc,delalloc`. bcachefs does not
implement `FIEMAP`, so duperemove falls back to reading every file in fixed
128 KiB blocks and building its own hash table — tens of millions of digests for
a home of this size, plus a comparison phase — while still being unable to align
on anything the filesystem actually stores.

Measured on the machine this was written for, that is the difference between
"hours" and "23 seconds".

## What bcachefs gives you instead

`FIDEDUPERANGE` — the same ioctl duperemove eventually calls to do the sharing —
works fine, and its *comparison* step is free, because bcachefs already stores a
checksum for every extent:

* identical contents → `bytes_deduped = length`, `status = 0`
* contents that differ everywhere → `bytes_deduped = 0`, `status = 1` (`DIFFERS`)
* contents that differ in one block → `bytes_deduped = 0`, `status = -EINVAL`

So the filesystem answers the hard question itself. Nothing needs to be read on
the user side, and nothing needs to be hashed: the kernel compares stored
checksums and remaps extents. The work is metadata-only, which is why the
dedupe phase runs at hundreds of megabytes per second per core and scales with
the *number of extents*, not the number of bytes.

## The alternative that is not one

| Tool | Why it does not help here |
|---|---|
| `duperemove` | needs `FIEMAP`; degrades to hashing everything |
| `bees` | its extent scan is btrfs-specific (`BTRFS_IOC_TREE_SEARCH`) |
| `rmlint --dedupe` | still hashes the whole content, only faster than duperemove |
| `jdupes -L` | hashes everything *and* replaces extents with hardlinks, i.e. one inode for several paths — fine for media, wrong for anything that can be edited |
| `hardlink` | same, and no reflink awareness at all |

All of them solve the problem by reading the data. bcachefs does not need the
data to be read.

## One caveat, verified

`bytes_deduped` is an **upper bound**, not a measurement. On bcachefs 1.39.5,
two 32 MiB files differing by 4 KiB at offset 10 MiB, deduplicated over a 16 MiB
request, are answered with `bytes_deduped = 16777216, status = 0` — the kernel
shares the parts that match and reports the whole requested range.

The data is not damaged: byte-for-byte comparison of both files before and after
shows them untouched, and the differing 4 KiB is still there. The number is
optimistic, not the operation. For a trustworthy figure, ask the filesystem:

```sh
bcachefs fs usage -f replicas /
```

`bchdedupe apply` prints both — what the kernel claimed, and what filesystem
accounting moved — and the second one is the one to quote.

### It is not idempotent either

Calling dedupe on two files that already share their extents reports the full
length again. Three calls in a row on a 16 MiB pair answered `16777216` each
time, with no further space freed after the first.

So a second pass over the same tree claims the same bytes a second time while
changing nothing on disk. The claimed total is not even a property of one run: it
counts successful ioctls, not space.

### How far off it can be, in the field

A real run over 251 groups on a 4 TB home, with the machine otherwise idle:

```
1x before: 3758433726976
491 files deduplicated, 171.4 GB reclaimed in 1053.5s (162.7 MB/s)
1x after:  3675168236032
```

The kernel claimed **171.4 GB**; filesystem accounting moved **83.3 GB** over the
same window (which also contained the usual churn: a Nix build, test runs). So on
that workload the claim was roughly twice the truth. The tool prints both numbers
for that reason, and the docs, the README and the commit messages all say the
same thing: the second number is the result, the first is a claim.
