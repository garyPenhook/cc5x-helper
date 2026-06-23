"""Filesystem helpers shared across the CLI, GUI, and manifest layer."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, encoding: str = "latin-1") -> None:
    """Write ``text`` to ``path`` atomically, never leaving a truncated/partial file.

    Encodes up front so an encoding error is raised *before* the destination is touched,
    then writes a sibling temp file and ``os.replace``s it into place (atomic on the same
    filesystem). A crash or error mid-write leaves the original file intact — a plain
    ``write_text`` opens the real file for truncation first, so any failure zeroes it.
    """
    data = text.encode(encoding)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp creates the temp at 0600; preserve the original file's permission bits so a
        # synced file does not silently lose its group/other access (a plain in-place write
        # would have kept them).
        try:
            os.chmod(tmp_name, os.stat(path).st_mode)
        except OSError:
            pass  # path did not exist (first write) or stat/chmod unsupported — leave default
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
