"""Bounded Unix socket framing and authenticated operating-system peer IDs."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import socket
import stat
import struct
import sys
import time
from functools import lru_cache
from pathlib import Path

MAX_FRAME = 262144


@lru_cache(maxsize=1)
def _mac_acl_library():
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.acl_get_file.restype = ctypes.c_void_p
    library.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    return library


def check_no_extended_acl(path: Path) -> None:
    """POSIX mode bits do not describe macOS named-user ACL grants.

    Reject extended ACLs altogether rather than attempting an incomplete
    effective-permission evaluator. Missing/unreadable inspection fails closed.
    """
    if sys.platform == "darwin":
        library = _mac_acl_library()
        ctypes.set_errno(0)
        acl = library.acl_get_file(os.fsencode(path), 0x100)  # ACL_TYPE_EXTENDED
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:
                return  # Existing lstat-verified path has no extended ACL.
            raise ValueError("Cannot inspect custody extended ACL: " + str(path))
        try:
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = library.acl_get_entry(acl, 0, ctypes.byref(entry))  # ACL_FIRST_ENTRY
            if result == 0 and entry.value:
                raise ValueError("Custody path has an extended ACL: " + str(path))
            if result != -1 or ctypes.get_errno() != errno.ENOENT:
                raise ValueError("Cannot inspect custody ACL entries: " + str(path))
        finally:
            library.acl_free(acl)
    elif sys.platform == "linux":
        for name in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                os.getxattr(path, name, follow_symlinks=False)
            except OSError as error:
                if error.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    continue
                raise ValueError("Cannot inspect custody extended ACL: " + str(path)) from None
            raise ValueError("Custody path has an extended ACL: " + str(path))


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate custody JSON key")
        value[key] = item
    return value


def peer_uid(connection: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.unpack("3i", credentials)[1]
    if hasattr(connection, "getpeereid"):
        return connection.getpeereid()[0]
    if os.uname().sysname == "Darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.CDLL(None, use_errno=True).getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError("Cannot authenticate custody peer")
        return uid.value
    raise OSError("This platform has no supported Unix peer identity API")


def receive(connection: socket.socket, *, timeout_seconds: float = 10) -> dict:
    data = bytearray()
    original_timeout = connection.gettimeout()
    deadline = time.monotonic() + timeout_seconds
    try:
        while len(data) <= MAX_FRAME:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Custody frame deadline exceeded")
            connection.settimeout(min(original_timeout or timeout_seconds, remaining))
            chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(data)))
            if not chunk:
                raise ValueError("Incomplete custody frame")
            data.extend(chunk)
            if len(data) > MAX_FRAME:
                raise ValueError("Custody frame is too large")
            if b"\n" in chunk:
                if not data.endswith(b"\n") or data.count(b"\n") != 1:
                    raise ValueError("Invalid custody frame")
                value = json.loads(data, object_pairs_hook=_unique_object)
                if not isinstance(value, dict):
                    raise ValueError("Custody frame must be an object")
                return value
        raise ValueError("Custody frame is too large")
    finally:
        connection.settimeout(original_timeout)


def send(connection: socket.socket, value: dict) -> None:
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(payload) > MAX_FRAME:
        raise ValueError("Custody frame is too large")
    connection.sendall(payload)


def check_owned_path(path: Path, *, owner: int, private: bool = False) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != owner or info.st_mode & (0o077 if private else 0o022):
        raise ValueError("Custody path has unsafe ownership or permissions: " + str(path))
    check_no_extended_acl(path)


def check_ancestors(path: Path, *, allowed_owners: set[int]) -> None:
    for parent in [path, *path.parents]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in allowed_owners or info.st_mode & 0o022:
            raise ValueError("Custody ancestor is writable or has unsafe ownership: " + str(parent))
        check_no_extended_acl(parent)
