"""Allow ``python -m bchdedupe``."""

from bchdedupe.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
