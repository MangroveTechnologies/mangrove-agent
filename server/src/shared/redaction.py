"""Small dependency-free helpers for shareable diagnostic metadata."""
from __future__ import annotations

import re

from src.shared.urls import strip_query

_ADDRESS = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
_URL = re.compile(r"https?://[^\s<>\"']+")
_SENSITIVE = {"secret", "private_key", "mnemonic", "seed_phrase", "password",
              "api_key", "authorization", "payment-signature", "x-payment", "vault_token"}


def redact_diagnostics(value):
    if isinstance(value, dict):
        return {str(k): "[redacted]" if str(k).lower() in _SENSITIVE else redact_diagnostics(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_diagnostics(v) for v in value]
    if isinstance(value, str):
        value = _URL.sub(lambda m: strip_query(m.group()) or "[invalid URL]", value)
        return _ADDRESS.sub(lambda m: m.group()[:6] + "…" + m.group()[-4:], value)
    return value


def redact_event(_, __, event_dict):
    return redact_diagnostics(event_dict)
