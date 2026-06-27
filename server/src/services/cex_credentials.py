"""Encrypted-at-rest store for CEX (Kraken) BYOK API credentials.

The user's API key + secret are Fernet-encrypted with the agent's master key
and written to ``agent-data/cex-<venue>.enc`` (chmod 600). They never leave the
machine and never enter chat. Mirrors the wallet-secret custody model, but
file-based so it needs no DB migration.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.config import app_config
from src.shared.crypto import fernet


def _agent_data_dir() -> Path:
    db_path = str(getattr(app_config, "DB_PATH", "./agent-data/agent.db"))
    return Path(db_path).parent


def _path(venue: str) -> Path:
    return _agent_data_dir() / f"cex-{venue}.enc"


def save(venue: str, api_key: str, api_secret: str) -> None:
    if not api_key or not api_secret:
        raise ValueError("api_key and api_secret are both required")
    blob = json.dumps({"api_key": api_key, "api_secret": api_secret}).encode()
    enc = fernet.encrypt(blob)
    p = _path(venue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(enc)
    p.chmod(0o600)


def load(venue: str) -> tuple[str, str] | None:
    """Return (api_key, api_secret) or None if not connected."""
    p = _path(venue)
    if not p.exists():
        return None
    data = json.loads(fernet.decrypt(p.read_bytes()))
    return data["api_key"], data["api_secret"]


def is_connected(venue: str) -> bool:
    return _path(venue).exists()


def disconnect(venue: str) -> bool:
    p = _path(venue)
    if p.exists():
        p.unlink()
        return True
    return False
