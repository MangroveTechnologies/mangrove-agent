"""Fail-closed deployment checks shared by daemon and human administration."""
from __future__ import annotations

import json
import os
import pwd
import stat
import sys
from pathlib import Path

from src.custody.transport import check_ancestors, check_owned_path


def initialize(config_path: str) -> dict:
    if os.name != "posix" or sys.platform not in {"linux", "darwin"}:
        raise ValueError("Isolated custody supports Linux and macOS only")
    if not sys.flags.isolated:
        raise ValueError("Custody must start with Python -I and the protected custody-service.py launcher")
    path = Path(config_path)
    if not path.is_absolute():
        raise ValueError("Use an absolute custody policy path")
    check_owned_path(path, owner=0)
    check_ancestors(path.parent, allowed_owners={0})
    settings = json.loads(path.read_text())
    custody_uid, agent_uid = settings["custody_uid"], settings["agent_uid"]
    if (type(custody_uid) is not int or type(agent_uid) is not int
            or custody_uid <= 0 or agent_uid <= 0 or custody_uid == agent_uid
            or os.geteuid() != custody_uid):
        raise ValueError("Run custody as its dedicated non-root UID, distinct from the agent UID")
    if sys.platform == "linux":
        status = Path("/proc/self/status").read_text().splitlines()
        capabilities = next(line.split()[1] for line in status if line.startswith("CapEff:"))
        if int(capabilities, 16):
            raise ValueError("Custody must run without Linux capabilities")
    # Root-owned files alone are insufficient when a writable parent can
    # replace their directory entry. Check every import tree and symlink target.
    scanned = set()
    def immutable_tree(start):
        pending = [start]
        while pending:
            candidate = pending.pop().resolve(strict=True)
            check_ancestors(candidate, allowed_owners={0})
            if not candidate.is_dir() or candidate in scanned:
                continue
            scanned.add(candidate)
            for imported in candidate.iterdir():
                target = imported.resolve(strict=True)
                check_owned_path(target, owner=0)
                if imported.is_symlink():
                    check_ancestors(target, allowed_owners={0})
                if target.is_dir():
                    pending.append(target)
    for raw in [sys.executable, *sys.path, Path(__file__).resolve().parents[3]]:
        if raw:
            candidate = Path(raw).resolve()
            if candidate.exists():
                immutable_tree(candidate)
    state = Path(settings["state_dir"])
    socket_path = Path(settings["socket_path"])
    if not state.is_absolute() or not socket_path.is_absolute():
        raise ValueError("Custody state and socket paths must be absolute")
    check_owned_path(state, owner=custody_uid, private=True)
    check_ancestors(state.parent, allowed_owners={0})
    check_owned_path(socket_path.parent, owner=custody_uid)
    check_ancestors(socket_path.parent.parent, allowed_owners={0})
    for candidate in state.iterdir():
        check_owned_path(candidate, owner=custody_uid, private=True)
        if not stat.S_ISREG(candidate.lstat().st_mode):
            raise ValueError("Custody state must contain only private regular files")
    app_path = path.parent / "config/custody-config.json"
    check_owned_path(app_path, owner=0)
    check_ancestors(app_path.parent, allowed_owners={0})
    application = json.loads(app_path.read_text())
    if (application.get("AUTH_ENABLED") is not True
            or Path(application["DB_PATH"]) != state / "wallets.db"
            or Path(application["MASTER_KEY_PATH"]) != state / "master.key"
            or application.get("CUSTODY_SOCKET")):
        raise ValueError("Custody application configuration is unsafe")
    os.environ["ENVIRONMENT"] = "custody"
    os.environ.pop("APP_ENV", None)
    os.environ["MANGROVE_AGENT_HOME"] = str(path.parent)
    os.umask(0o077)
    from src.custody import runtime
    from src.shared.db.sqlite import init_db

    runtime._active = True
    runtime._admin_command_prefix = (
        "sudo", "-u", pwd.getpwuid(custody_uid).pw_name, sys.executable, "-I",
        str(Path(__file__).resolve().parents[3] / "scripts/custody-service.py"),
        "admin", "--config", str(path),
    )
    init_db()
    return settings
