"""SDK client singletons — mangroveai + mangrovemarkets.

Both clients are initialized lazily on first access and cached for the
lifetime of the process. Routes and services import the accessors, never
instantiate clients themselves. That keeps test mocking easy (override the
@lru_cache'd function) and avoids multiple HTTP pools / auth re-inits.

Usage:
    from src.shared.clients.mangrove import mangrove_ai_client, mangrove_markets_client

    signals = mangrove_ai_client().signals.list()
    venues = mangrove_markets_client().dex.supported_venues()
"""
from __future__ import annotations

from functools import lru_cache

import httpx
from mangrove_ai import MangroveAI
from mangrove_markets import MangroveMarkets


def _get_config():
    """Lazy import to avoid circular imports during testing."""
    from src.config import app_config
    return app_config


@lru_cache(maxsize=1)
def mangrove_ai_client() -> MangroveAI:
    """Return the singleton MangroveAI SDK client.

    Reads MANGROVE_API_KEY from config. Environment (dev vs prod) is
    auto-detected by the SDK from the API key prefix (dev_* / prod_*).

    The SDK's default request timeout is 30s, which is shorter than a
    full backtest via Oracle's /api/v1/backtest (observed 52-76s during
    Cloud Run cold-starts + multi-month lookback windows). We raise the
    client-level timeout so long-running calls complete instead of
    silently timing out at the agent/tool layer. Other endpoints
    (kb_search, signals.list, get_ohlcv) normally return in <2s; the
    higher ceiling only kicks in when something upstream is genuinely
    slow.
    """
    config = _get_config()
    return MangroveAI(
        api_key=str(config.MANGROVE_API_KEY),
        timeout=float(config.MANGROVE_SDK_TIMEOUT_SECONDS),
    )


@lru_cache(maxsize=1)
def mangrove_markets_client() -> MangroveMarkets:
    """Return the singleton MangroveMarkets SDK client.

    Reads MANGROVEMARKETS_BASE_URL and MANGROVE_API_KEY from config. The
    base URL points at the MangroveMarkets MCP server (DEX + wallet +
    portfolio endpoints).
    """
    config = _get_config()
    return MangroveMarkets(
        base_url=str(config.MANGROVEMARKETS_BASE_URL),
        api_key=str(config.MANGROVE_API_KEY),
    )


def reset_clients() -> None:
    """Clear the cached singletons. Tests use this to re-init with different config."""
    mangrove_ai_client.cache_clear()
    mangrove_markets_client.cache_clear()


def create_x402_mangrove_client(
    *,
    environment: str,
    base_url: str,
    kb_base_url: str,
    wallet_address: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> MangroveAI:
    """Build an explicit payment client; the key-mode singleton is unchanged.

    B5 owns automatic auth-mode selection. Until then callers must explicitly
    choose this factory and the upstream URLs/environment. No SDK default may
    select production for a keyless caller. Close this client after use (or use
    it as a context manager); unlike the key-mode accessor it is not cached.

    An ambient API key inherited by mangroveai 1.16 is rejected at the transport
    boundary before any request goes out. We neither mutate process environment
    nor rely on SDK-private auth fields to suppress it.
    """
    from src.shared.errors import ValidationError
    from src.shared.x402.sync_transport import X402SyncTransport

    if environment not in {"local", "dev", "prod"}:
        raise ValidationError("An explicit local, dev, or prod environment is required for x402.")
    timeout = float(_get_config().MANGROVE_SDK_TIMEOUT_SECONDS)
    payment_transport = X402SyncTransport(
        wallet_address=wallet_address,
        allowed_origins=(base_url, kb_base_url),
        transport=transport,
        timeout=timeout,
    )
    http = httpx.Client(
        transport=payment_transport, timeout=timeout,
        follow_redirects=False, trust_env=False,
    )
    try:
        return MangroveAI(
            api_key=None, environment=environment,
            base_url=base_url, kb_base_url=kb_base_url,
            load_dotenv=False, auto_retry=False, auto_auth=False,
            timeout=timeout, httpx_client=http,
        )
    except Exception:
        http.close()
        raise
