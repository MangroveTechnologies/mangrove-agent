"""SDK client singletons — mangroveai + mangrovemarkets.

Both clients are initialized lazily on first access and cached for the
lifetime of the process. Routes and services import the accessors, never
instantiate clients themselves. That keeps test mocking easy (override the
accessor function) and avoids multiple HTTP pools / auth re-inits.

Usage:
    from src.shared.clients.mangrove import mangrove_ai_client, mangrove_markets_client

    signals = mangrove_ai_client().signals.list()
    venues = mangrove_markets_client().dex.supported_venues()
"""
from __future__ import annotations

from threading import RLock

import httpx
from mangrove_ai import MangroveAI
from mangrove_markets import MangroveMarkets

from src.shared.errors import ValidationError

_clients: dict[str, MangroveAI | MangroveMarkets] = {}
_clients_lock = RLock()


def _get_config():
    """Lazy import to avoid circular imports during testing."""
    from src.config import app_config
    return app_config


def mangrove_ai_client() -> MangroveAI:
    """Return the singleton MangroveAI SDK client.

    A configured upstream key selects key mode, otherwise x402 is selected.
    Authentication failures never switch modes or trigger a wallet payment.
    Configuration is process-scoped; restart after changing authentication mode.

    The SDK's default request timeout is 30s, which is shorter than a
    full backtest via Oracle's /api/v1/backtest (observed 52-76s during
    Cloud Run cold-starts + multi-month lookback windows). We raise the
    client-level timeout so long-running calls complete instead of
    silently timing out at the agent/tool layer. Other endpoints
    (kb_search, signals.list, get_ohlcv) normally return in <2s; the
    higher ceiling only kicks in when something upstream is genuinely
    slow.
    """
    with _clients_lock:
        if "ai" not in _clients:
            config = _get_config()
            key = _api_key(config)
            if key:
                client = MangroveAI(
                    api_key=key, load_dotenv=False,
                    timeout=float(config.MANGROVE_SDK_TIMEOUT_SECONDS),
                )
            else:
                client = create_x402_mangrove_client(**_payment_destination(config))
            _clients["ai"] = client
        return _clients["ai"]


def _api_key(config) -> str | None:
    value = getattr(config, "MANGROVE_API_KEY", None)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("MANGROVE_API_KEY must be a string or null.")
    key = value.strip()
    return None if key.lower() in {"", "none", "null"} else key


def _payment_setting(config, name: str) -> str | None:
    value = getattr(config, name, None)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"{name} must be a string or null.",
            suggestion="Remove this advanced override to use the built-in service destination.",
        )
    value = value.strip()
    return None if value.lower() in {"", "none", "null"} else value


def _payment_destination(config) -> dict[str, str]:
    """Resolve reviewed defaults and optional developer overrides.

    The desktop agent runs in local mode but uses hosted services. A local
    backend is an explicit override. Payment network selection is independent:
    choosing a service never changes X402_NETWORK or the selected wallet.
    """
    catalog = config.MANGROVE_ENDPOINTS
    environment = _payment_setting(config, "X402_MANGROVE_ENVIRONMENT")
    if environment is None:
        environment = catalog["default_environment"].get(config.ENVIRONMENT)
    if environment not in catalog["environments"]:
        raise ValidationError(
            "No supported MangroveAI service environment selected.",
            suggestion="Use an agent local/dev/test/prod environment, or set the advanced service override to local/dev/prod.",
        )
    defaults = catalog["environments"][environment]
    return {
        "environment": environment,
        "base_url": _payment_setting(config, "X402_MANGROVE_BASE_URL") or defaults["base_url"],
        "kb_base_url": _payment_setting(config, "X402_MANGROVE_KB_BASE_URL") or defaults["kb_base_url"],
    }


def mangrove_markets_client() -> MangroveMarkets:
    """Return the singleton MangroveMarkets SDK client.

    Reads MANGROVEMARKETS_BASE_URL and MANGROVE_API_KEY from config. The
    base URL points at the MangroveMarkets MCP server (DEX + wallet +
    portfolio endpoints).
    """
    with _clients_lock:
        if "markets" not in _clients:
            config = _get_config()
            _clients["markets"] = MangroveMarkets(
                base_url=str(config.MANGROVEMARKETS_BASE_URL),
                api_key=_api_key(config),
            )
        return _clients["markets"]


def reset_clients() -> None:
    """Close and clear clients after callers have stopped (tests or shutdown).

    This is not a live configuration reload: closing an in-flight pool is unsafe.
    The lock serializes first access so concurrent callers cannot leak pools.
    """
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
        for client in clients:
            client.close()


def create_x402_mangrove_client(
    *,
    environment: str,
    base_url: str,
    kb_base_url: str,
    wallet_address: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> MangroveAI:
    """Build a payment client for the singleton or an explicit diagnostic call.

    The automatic factory resolves bundled defaults before calling this builder;
    diagnostics may choose destinations explicitly. No ambient SDK default may
    select a destination. Close this client after use (or use
    it as a context manager); unlike the key-mode accessor it is not cached.

    An ambient API key inherited by mangroveai 1.16 is rejected at the transport
    boundary before any request goes out. We neither mutate process environment
    nor rely on SDK-private auth fields to suppress it.
    """
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
