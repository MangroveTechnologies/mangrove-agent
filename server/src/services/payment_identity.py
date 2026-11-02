"""Read-only identity of the configured default x402 payer.

Use the payment path's resolver, never wallet ordering, labels or balances.
Only authenticated surfaces expose this wallet metadata. No signing, secret
retrieval, balance calls or budget changes are performed here.
"""
from src.config import app_config
from src.shared.db.sqlite import get_connection
from src.shared.errors import ValidationError
from src.shared.x402.config import get_network, get_payer_wallet


def configured_payer_address() -> str | None:
    if not get_payer_wallet().strip():
        return None
    from src.services.x402_payer import resolve_payer_wallet

    try:
        return resolve_payer_wallet()
    except ValidationError:
        # Never echo an invalid raw config value (it could contain a secret).
        return None


def get_status() -> dict:
    address = configured_payer_address()
    network = get_network()
    mode = getattr(app_config, "MANGROVE_ACCESS_MODE", None)
    if mode is None:
        from src.shared.clients.mangrove import _api_key
        mode = "api-key" if _api_key(app_config) else "x402"
    if mode not in {"api-key", "oauth", "x402"}:
        mode = "invalid"
    wallet = None
    if address:
        wallet = get_connection().execute(
            "SELECT backup_confirmed_at FROM wallets WHERE lower(address) = lower(?) AND chain = 'evm'",
            (address,),
        ).fetchone()
    status = ("configured" if wallet is not None else "wallet_not_found") if address else (
        "invalid_configuration" if get_payer_wallet().strip() else "not_configured"
    )
    return {
        "address": address,
        "selection_source": "configuration" if get_payer_wallet().strip() else None,
        "selection_status": status,
        "scope": "default_x402_payer",
        "access_mode": mode,
        "network": network,
        "network_name": {"eip155:8453": "Base Mainnet", "eip155:84532": "Base Sepolia"}.get(network),
        "wallet_stored": wallet is not None,
        "backup_confirmed": bool(wallet["backup_confirmed_at"]) if wallet is not None else None,
        "balance_checked": False,
    }
