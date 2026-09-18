"""Shared CLI for the four custodied-wallet payment demonstrations.

No application boot, migrations, wallet creation, or budget reset takes place.
Config imports are deferred so --help works without credentials or state.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))


class DemoError(Exception):
    """A safe, actionable CLI diagnostic containing no remote error text."""


def parser(mode: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=f"Custodied x402 {mode} demo against an existing local agent. May spend USDC.",
        epilog="Set ENVIRONMENT (and MANGROVE_AGENT_HOME for a plugin install) to match the running agent.",
    )
    result.add_argument("--server-url", help="Local agent origin; defaults to configured LOCAL_AGENT_URL.")
    result.add_argument("--wallet", help="Public address of an existing wallet; defaults to X402_PAYER_WALLET.")
    result.add_argument("--state-dir", type=Path, default=REPO_ROOT,
                        help="Anchor for relative state paths; defaults to repository root, as in setup.sh.")
    result.add_argument("--allow-mainnet", action="store_true",
                        help="Explicitly allow real USDC spending if configuration selects Base mainnet.")
    return result


def local_origin(value: str) -> str:
    import httpx

    try:
        url = httpx.URL(value)
        if (url.scheme not in {"http", "https"} or url.host not in {"localhost", "127.0.0.1", "::1"}
                or url.userinfo or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise DemoError("Use an HTTP(S) loopback origin without credentials, query, fragment or path.") from None
    return str(url).rstrip("/")


def existing_state(config, state_dir: Path) -> None:
    """Fail before wallet access if the selected database is absent/outdated.

    Match setup.sh's repository-root cwd by default; plugin config already
    anchors paths to MANGROVE_AGENT_HOME. Never create an empty database here.
    """
    for key in ("DB_PATH", "MASTER_KEY_PATH"):
        value = getattr(config, key, None)
        if value and str(value) != ":memory:":
            path = Path(str(value)).expanduser()
            setattr(config, key, str((state_dir / path).resolve()))
    path = Path(str(config.DB_PATH))
    if str(config.DB_PATH) == ":memory:" or not path.is_file():
        raise DemoError("Existing agent database not found. Match the running agent's config and state directory.")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            applied = {row[0] for row in conn.execute("SELECT filename FROM _migrations")}
        required = {p.name for p in (SERVER_ROOT / "src/shared/db/migrations").glob("*.sql")}
        if not required.issubset(applied):
            raise DemoError("Agent database needs migration. Start the updated agent before running this script.")
    except sqlite3.Error:
        raise DemoError("Cannot read the agent database. Check the selected config and start the agent first.") from None


async def inspect_quote(url: str, network: str) -> None:
    """Show a bounded, unsigned challenge from the actual running REST server."""
    import httpx
    from x402.schemas.payments import PaymentRequired

    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as http:
        async with http.stream("GET", url) as response:
            if response.status_code != 402:
                raise DemoError("Expected an unsigned HTTP 402 challenge from the running agent.")
            raw = response.headers.get("payment-required", "")
            if not raw or len(raw) > 16384:
                raise DemoError("Missing or oversized payment challenge.")
            try:
                decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
                required = PaymentRequired.model_validate_json(decoded)
            except (ValueError, RecursionError):
                raise DemoError("Malformed payment challenge; no payment attempted.") from None
    choices = [entry for entry in required.accepts if entry.network == network and entry.scheme == "exact"]
    if not choices:
        raise DemoError("Server challenge does not match the configured payment network; no payment attempted.")
    print("HTTP 402: payment required. Advertised requirements (revalidated by the payer before signing):")
    for entry in choices[:8]:
        print(json.dumps({"network": entry.network, "asset": entry.asset,
                          "payTo": entry.pay_to, "amount_base_units": entry.amount}, ensure_ascii=True))


@contextmanager
def quiet_mcp_diagnostics():
    """Keep SDK wire payloads and remote exception text out of CLI diagnostics.

    This standalone CLI reports its own safe errors. Restore logging on exit;
    the running agent's logging configuration is never changed.
    """
    logger = logging.getLogger("mcp")
    propagate = logger.propagate
    handler = logging.NullHandler()
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


async def mcp_payment(origin: str, wallet: str | None):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from src.services import x402_payer

    # No proxy environment, credentials or redirects. An overall deadline also
    # bounds a live SSE stream; an HTTP read timeout alone would not do so.
    with quiet_mcp_diagnostics():
        async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False, max_redirects=0) as http:
            async with streamable_http_client(origin + "/mcp/", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    return await x402_payer.pay_mcp(
                        session, wallet_address=wallet, resource=origin + "/mcp/",
                    )


def report(result, *, mcp: bool = False) -> int:
    """Never equate delivered content or a sent signature with settlement."""
    print(f"Resource status: {result.status_code}")
    if result.paid:
        print("Settlement receipt validated (server-reported; not independently checked on chain).")
        print(f"Payer: {result.payer}\nNetwork: {result.network}\nTransaction: {result.transaction}")
        explorer = {"eip155:8453": "https://basescan.org/tx/",
                    "eip155:84532": "https://sepolia.basescan.org/tx/"}.get(result.network)
        if explorer:
            print(f"Explorer: {explorer}{result.transaction}")
    else:
        print("Settlement unconfirmed. Inspect the payment ledger before retrying; no budget was released.")
    if result.status_code != 200 or not result.paid:
        return 1
    body = result.body
    if mcp:
        try:
            body = json.loads(body[0].text) if body else None
        except (AttributeError, ValueError, TypeError, RecursionError):
            body = None
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        # Quote and bound display text; never dump arbitrary error responses.
        print("Message: " + json.dumps(body["message"][:500], ensure_ascii=True))
    return 0


async def run(mode: str, args) -> int:
    from src.config import app_config
    from src.shared.logging import configure

    # Standalone scripts do not run the app lifespan, which normally installs
    # redaction. Configure it before importing/using the wallet services.
    configure(str(app_config.ENVIRONMENT))
    from src.services import x402_payer
    from src.shared.crypto.fernet import require_existing_master_key

    origin = local_origin(args.server_url or app_config.LOCAL_AGENT_URL)
    network = x402_payer.get_network()
    if network not in {"eip155:84532", "eip155:8453"}:
        raise DemoError("Configure X402_NETWORK explicitly as Base Sepolia or Base; no network fallback is used.")
    if network == "eip155:8453" and not args.allow_mainnet:
        raise DemoError("Base mainnet spends real USDC. Pass --allow-mainnet only if that is intended.")
    existing_state(app_config, args.state_dir)
    require_existing_master_key()
    print(f"Server: {origin}\nConfigured network: {network}")
    url = origin + "/api/x402/hello-mangrove"
    if mode in {"walkthrough", "smoke"}:
        if mode == "walkthrough":
            input("Step 1: request an unsigned payment challenge. Press Enter...")
        await inspect_quote(url, network)
        if mode == "walkthrough":
            input("Step 2: request again and pay from the custodied wallet within its spending limits. Enter to proceed, Ctrl+C to cancel...")
    async with asyncio.timeout(120):
        result = (await mcp_payment(origin, args.wallet) if mode == "mcp"
                  else await x402_payer.pay(url, wallet_address=args.wallet))
    return report(result, mcp=mode == "mcp")


def main(mode: str, argv: list[str] | None = None) -> int:
    args = parser(mode).parse_args(argv)
    try:
        return asyncio.run(run(mode, args))
    except (KeyboardInterrupt, EOFError):
        print("Cancelled. Inspect the payment ledger if signing had already started.", file=sys.stderr)
        return 130
    except DemoError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        from src.shared.errors import AgentError

        if isinstance(error, AgentError):
            print(f"ERROR: {error.code}. Check wallet selection, backup confirmation, network and spending limits.", file=sys.stderr)
        else:
            print("ERROR: Payment check failed. Check configuration and agent availability.", file=sys.stderr)
        print("Inspect the payment ledger before retrying; an interrupted payment may have settled.", file=sys.stderr)
        return 1
