"""bchdedupe — hash-free deduplication for bcachefs.

The package exposes two operations: :func:`scan` finds files that *look* like
duplicates, and :func:`apply_groups` asks the filesystem to actually share their
extents. Nothing here ever deletes or rewrites file content.
"""

from bchdedupe.core import (
    DEDUPE_CHUNK,
    SLICE,
    ApplyResult,
    FileEntry,
    Group,
    ScanResult,
    apply_groups,
    dedupe_pair,
    dedupe_range,
    filesystem_used,
    human,
    parse_size,
    scan,
    signature,
)

__version__ = "0.1.0"

__all__ = [
    "DEDUPE_CHUNK",
    "SLICE",
    "ApplyResult",
    "FileEntry",
    "Group",
    "ScanResult",
    "__version__",
    "apply_groups",
    "dedupe_pair",
    "dedupe_range",
    "filesystem_used",
    "human",
    "parse_size",
    "scan",
    "signature",
]
