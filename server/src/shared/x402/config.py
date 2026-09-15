"""x402 payment configuration -- reads from app_config singleton.

All values come from the per-environment JSON config files via the
standard config system. No os.environ, no hardcoded values.
"""


def _get_config():
    """Lazy import to avoid circular imports."""
    from src.config import app_config
    return app_config


def get_facilitator_url() -> str:
    return str(_get_config().X402_FACILITATOR_URL)


def get_network() -> str:
    return str(_get_config().X402_NETWORK)


def get_pay_to() -> str:
    return str(_get_config().X402_PAY_TO)


def get_usdc_contract() -> str:
    return str(_get_config().X402_USDC_CONTRACT)


def get_hello_mangrove_price() -> str:
    return str(_get_config().X402_HELLO_MANGROVE_PRICE)


def get_payer_wallet() -> str:
    """Address outbound payments are signed with. Empty when unset."""
    return str(_get_config().X402_PAYER_WALLET or "")


def get_cdp_api_key_id() -> str:
    return str(_get_config().X402_CDP_API_KEY_ID or "")


def get_cdp_api_key_secret() -> str:
    return str(_get_config().X402_CDP_API_KEY_SECRET or "")
