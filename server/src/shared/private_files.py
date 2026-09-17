"""Create local sensitive files with owner-only permissions from the outset."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def private_fd(path, *, append=False):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if append:
        flags |= os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Sensitive state must be a regular file")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def append_private(path, line):
    with os.fdopen(private_fd(path, append=True), "a", encoding="utf-8") as stream:
        stream.write(line + "\n")
