"""Crash- and disk-full-safe file writes."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Union


def atomic_write_text(path: Union[str, Path], text: str, encoding: str = "utf-8") -> Path:
    """Write to a temp file in the same folder, fsync, then rename over `path`.

    If the disk fills up (or the process dies) halfway through, the old file
    is left intact instead of truncated to zero bytes -- which is what a
    plain `write_text` does, and what wiped the sweep checkpoints once.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def free_mb(path: Union[str, Path]) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 1_048_576
