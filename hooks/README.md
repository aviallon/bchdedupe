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

## The gap CI cannot close

`FIDEDUPERANGE` is not implemented on ext4, which is what GitHub-hosted runners
use, so the filesystem-dependent tests skip there. What CI *does* verify is that
the suite collects and runs, and the pure-logic tests (chunk walk, master
rotation, clamping) run everywhere. The end-to-end proof — "the filesystem's own
accounting went down" — only runs on a machine with bcachefs, via
`pytest -m integration`.
