# AGENTS.md — bchdedupe

## What this project is

A deduplication tool for **bcachefs**, in Python, whose single trick is to let the
filesystem do the comparison: `FIDEDUPERANGE` compares stored extent checksums, so
nothing is hashed and no data is read on the user's side. It exists because
`duperemove` cannot work on bcachefs — no `FIEMAP` — and that is documented in
`docs/why-not-duperemove.md`, which is the project's ground truth for *why* the
design looks like this.

Two things about it are unusual and worth stating up front:

- **It writes to the user's filesystem.** Deduplication shares extents; it never
  deletes or rewrites content, but "it only touches metadata" is a claim, not a
  comfort. Every change must be verifiable from outside: assert the content is
  unchanged, and measure the space with the filesystem's own accounting rather
  than trusting our count.
- **The kernel's answer is not a measurement.** `bytes_deduped` is an upper bound
  (verified, see `docs/why-not-duperemove.md`). Code must never present it as an
  exact figure, and tests must not assert exact reclaimed bytes from it.

## Read this first (in order)

1. `docs/why-not-duperemove.md` — the `FIEMAP` finding, the ioctl's real
   semantics, and the over-report caveat. Everything else follows from it.
2. `src/bchdedupe/core.py` — the primitives, each carrying the observation it
   rests on in its docstring.
3. `src/bchdedupe/cli.py` — argument parsing and progress output, nothing more.
4. `tests/test_bchdedupe.py` — which tests need a real filesystem, and why the
   pure-logic ones exist.

## How to run anything

Everything goes through the pinned dev shell; it provides one Python
interpreter carrying pytest and mypy (a single interpreter matters: with the
tools from separate profiles, mypy cannot resolve pytest and reports the whole
test suite as untyped).

```sh
nix develop -c 'ruff format --check src tests && ruff check src tests && mypy && pytest -q'
nix develop -c 'pytest -m integration'   # needs bcachefs; measures real space
```

The hooks encode the same commands:

```sh
git config core.hooksPath hooks
```

`pre-commit` = format + lint + types. `pre-push` = the same plus the suite. Both
**fail loudly when a tool is missing** rather than passing silently.

## Commits

Conventional Commits, English, lowercase scope when there is one:

```
fix(probe): mark UDP endpoints unprobed instead of failing them
feat(cli): add a --dry-run that reports the estimate and touches nothing
perf(scan): index candidates with fd, 2m17s -> 23.5s
docs: explain why duperemove cannot work on bcachefs
```

The subject says what changed and, when it is not obvious, why. The body is
written the way you would explain the change to someone who was not there:

- what was wrong, or what was missing;
- what the change does, and the mechanism it relies on;
- how it was verified — the command, the measurement, from → to;
- what was rejected and why, when a plausible alternative exists.

Rules that are not negotiable:

- **Never claim verification you did not perform.** "Tests pass" is not "it
  works": name what was observed, where, and with what numbers.
- **Write the test that would fail if you are wrong**, then break the fix on
  purpose to confirm it does. A test that passes either way proves nothing.
- **Never state a number the code cannot back.** Reclaimed space comes from
  `bcachefs fs usage`, not from `bytes_deduped`.
- **A logged error is a failure**, and a gate that silently does nothing is worse
  than no gate.
- Signs commits, unless the signing prompt is unavailable — in which case say so
  in the commit body and in the report, and never disable signing silently.
- `Co-Authored-By: <model>` in the trailer when an AI wrote the change. Say which
  model. This is how the provenance stays visible in `git log`.

## Documentation

Prose is English, factual, and free of marketing. A docstring states the
*observation* the code depends on — a measured number, a kernel behaviour, an
ioctl's real semantics — not a restatement of the signature. When a decision
looks arbitrary, the docstring or the commit body says which measurement settled
it; if a measurement is missing, write one instead of guessing.

Update the doc when the code and the doc disagree: `docs/why-not-duperemove.md`
and the "Reading the output" section of the README are the two that go stale
first, because they contain numbers.

## Testing policy

- Tests that need `FIDEDUPERANGE` **skip** where it is absent (ext4, tmpfs) and
  are honest about it. `/tmp` is a tmpfs on many machines; the suite looks for a
  capable directory and honours `BCHDEDUPE_TEST_DIR`. CI mounts a loopback btrfs
  and points that variable at it, so the ioctl tests do run there — see
  `hooks/README.md` for what that buys and what it cannot.
- The integration test (`-m integration`) is deselected by default because it
  reads whole-filesystem counters, which move on their own. It runs in the btrfs
  CI job, where those counters are accurate.
- Do not build a test on `bcachefs fs usage`: it is lazy enough to be useless as
  an oracle (a 128 MiB deduplication moved it by nothing in 180 s). Use it to
  report, and measure end to end by hand.
- Pure-logic tests — the chunk walk, the master rotation, the clamp on the
  kernel's answer — run everywhere and are the ones that protect the algorithm.
  Keep them that way: they are the only part of the safety argument CI can check.
- A test that cannot fail is deleted, not kept.
