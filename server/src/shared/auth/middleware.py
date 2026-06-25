"""API key authentication middleware.

Validates X-API-Key header against configured API_KEYS (comma-separated).
When AUTH_ENABLED is false, all requests pass without validation.
API key holders bypass x402 payment requirements.
"""
import secrets
from contextvars import ContextVar
from typing import Optional

# Per-request X-API-Key captured from the HTTP header for the mounted MCP
# sub-app. FastMCP tools receive their arguments as JSON-RPC params and never
# see HTTP headers, so the `X-API-Key` header Claude Code sends (registered via
# `claude mcp add --header "X-API-Key: <key>"`) is bridged into this ContextVar
# by the MCP ASGI middleware (src/app.py) and consulted by the tools' auth gate
# (src/mcp/tools.py::_require) whenever no explicit api_key param is supplied.
_request_api_key: ContextVar[str] = ContextVar("request_api_key", default="")


def set_request_api_key(api_key: Optional[str]):
    """Store the current request's X-API-Key. Returns a token for reset()."""
    return _request_api_key.set(api_key or "")


def reset_request_api_key(token) -> None:
    """Reset the per-request X-API-Key (call in a finally after the request)."""
    _request_api_key.reset(token)


def get_request_api_key() -> str:
    """The X-API-Key captured from the current request's header, or ''."""
    return _request_api_key.get()


def _get_config():
    """Lazy import to avoid circular imports during testing."""
    from src.config import app_config
    return app_config


def _matches_any(api_key: str, valid_keys: set[str]) -> bool:
    """Constant-time comparison against the configured key set.

    Avoids the timing oracle in plain ``api_key in valid_keys``: the dict /
    set ``in`` operator falls through to ``str.__eq__``, which short-circuits
    on first byte mismatch. Iterating the full set with
    ``secrets.compare_digest`` and folding the result with a non-short-
    circuiting OR keeps cost independent of which key matches.
    """
    matched = False
    for k in valid_keys:
        if secrets.compare_digest(api_key, k):
            matched = True
    return matched


def validate_api_key(api_key: Optional[str]) -> Optional[str]:
    """Validate an API key against configured keys.

    Returns the valid key if authenticated, None if auth is disabled.
    Raises ValueError if key is missing or invalid when auth is enabled.
    """
    config = _get_config()

    if not config.AUTH_ENABLED:
        return None

    if not api_key:
        raise ValueError("Missing API key")

    valid_keys = {k.strip() for k in str(config.API_KEYS).split(",")}
    if not _matches_any(api_key, valid_keys):
        raise ValueError("Invalid API key")

    return api_key


def has_valid_api_key(api_key: Optional[str]) -> bool:
    """Check if an API key is valid without raising.

    Used by x402 middleware to determine if payment can be skipped.
    """
    try:
        config = _get_config()
        if not config.AUTH_ENABLED:
            return True
        if not api_key:
            return False
        valid_keys = {k.strip() for k in str(config.API_KEYS).split(",")}
        return _matches_any(api_key, valid_keys)
    except Exception:
        return False
