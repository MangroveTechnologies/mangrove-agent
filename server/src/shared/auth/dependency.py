"""FastAPI dependency for auth-gated agent endpoints.

Wraps the existing validate_api_key middleware function and translates
its ValueError into our standard AgentError types so the central
agent_error_handler returns the spec-shaped JSON.

Usage:
    from fastapi import Depends
    from src.shared.auth.dependency import require_api_key

    @router.get("/something", dependencies=[Depends(require_api_key)])
    async def something(): ...
"""
from __future__ import annotations

from fastapi import Header

from src.shared.auth.middleware import published_default_keys_configured, validate_api_key
from src.shared.errors import AuthInvalidApiKey, AuthMissingApiKey


def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> str | None:
    """FastAPI dependency: enforce X-API-Key or return the configured one.

    Returns the validated key (useful for per-user logic; v1 doesn't need
    it but routes may bind the value if they want).
    """
    try:
        return validate_api_key(x_api_key)
    except ValueError as e:
        msg = str(e).lower()
        if "missing" in msg:
            raise AuthMissingApiKey(
                "API key required.",
                suggestion="Pass X-API-Key header with a valid key.",
            ) from e
        if published_default_keys_configured():
            suggestion = (
                "This agent's API_KEYS still holds the key published in the example config, "
                "which is refused. Re-run ./scripts/setup.sh to generate a unique key."
            )
        else:
            suggestion = "Check your X-API-Key header against the configured API_KEYS."
        raise AuthInvalidApiKey("Invalid API key.", suggestion=suggestion) from e
