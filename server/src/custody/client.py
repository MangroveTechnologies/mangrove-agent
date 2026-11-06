"""Agent-side custody client. No local-secret fallback and no export operation."""
from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

from src.custody.transport import check_ancestors, peer_uid, receive, send
from src.shared.errors import SigningError


def configured() -> bool:
    from src.config import app_config
    return bool(getattr(app_config, "CUSTODY_SOCKET", None))


def call(operation: str, arguments: dict):
    from src.config import app_config
    raw_path = getattr(app_config, "CUSTODY_SOCKET", None)
    expected_uid = getattr(app_config, "CUSTODY_UID", None)
    if not raw_path or type(expected_uid) is not int or expected_uid in {0, os.geteuid()}:
        raise SigningError("Isolated wallet custody is not configured. A human administrator must install the custody service; legacy local signing is disabled.")
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError
        check_ancestors(path.parent, allowed_owners={0, expected_uid})
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != expected_uid:
            raise ValueError
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(str(path))
            if peer_uid(connection) != expected_uid:
                raise ValueError
            send(connection, {"operation": operation, "arguments": arguments})
            result = receive(connection)
    except (OSError, ValueError, TypeError):
        raise SigningError("The isolated custody service is unavailable or its OS identity could not be verified.") from None
    if result.get("ok") is not True:
        # The service returns a fixed public refusal, never arbitrary exception text.
        raise SigningError("The isolated custody service refused this operation. Review its protected policy and audit log in the administrator terminal.")
    return result.get("result")
