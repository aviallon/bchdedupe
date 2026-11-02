# Git hooks

Two hooks, committed here so a fresh clone can install them with one command:

```sh
git config core.hooksPath hooks
```

| Hook | Runs | When to expect it to bite |
|---|---|---|
| `pre-commit` | `ruff format --check`, `ruff check`, `mypy` | a couple of seconds |
| `pre-push` | the above, plus `pytest -q` | under a minute |

They are plain `sh`, no framework, so there is nothing to bootstrap and nothing
to keep in sync with a Python version.

## Why they fail when the tools are missing

If `ruff` or `mypy` is not on `PATH`, the hook **fails loudly** instead of
quietly passing. A gate that silently does nothing is worse than no gate: it
looks like verification and is not. Enter the dev shell, which has all of them
from a single pinned nixpkgs:

```sh
nix develop -c 'git commit -m "…"'
```

`git commit --no-verify` remains available for someone who really means it, and
CI runs exactly the same checks either way.

## What CI covers, and what it cannot

The runner's own ext4 filesystem cannot deduplicate, so CI mounts a loop-backed
**btrfs** (1 GiB image, `mkdir /mnt/dedupe`, `mount -o loop`) and points
`BCHDEDUPE_TEST_DIR` at it. `FIDEDUPERANGE` is generic — the VFS dispatches it to
whatever the filesystem implements — so btrfs exercises the same contract as
bcachefs, including the `DIFFERS` path and the end-to-end check that free space
comes back (`statvfs`, which btrfs reports accurately).

One thing btrfs cannot cover: bcachefs's own answer to the ioctl is optimistic
(it reports a whole range after sharing part of it). That is pinned by a unit
test that stubs the ioctl, and by the real-run numbers recorded in
`docs/why-not-duperemove.md`.

And one thing no automated test covers, deliberately: whether bcachefs actually
gave the space back. `bcachefs fs usage` is the only figure worth reading there,
and it is far too lazy to serve as an oracle — measured: a 128 MiB deduplication
moved it by nothing in 180 s, while a 17-minute run moved it by 83 GB. So the
end-to-end proof on bcachefs is a manual measurement, recorded with its numbers,
rather than a test that would fail at random.
