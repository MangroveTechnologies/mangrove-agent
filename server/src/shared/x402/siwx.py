"""Sign-In-With-X proofs for MangroveAI calls made in wallet (x402) mode.

x402's SIWX extension lets a wallet prove who it is without paying: it signs a short
EIP-4361 ("Sign-In with Ethereum") message naming the server's origin, a fresh nonce and
a five-minute window, and sends it base64-encoded in the ``SIGN-IN-WITH-X`` header.
MangroveAI then runs a free call as this wallet -- so a free per-caller read such as
``list_strategies`` returns this wallet's own data -- and counts its rate limit per
wallet rather than per IP. A payment still proves identity on its own; the proof never
stands in for one.

The message text is built here, not by the x402 library, whose EVM path needs the
optional ``signinwithethereum`` package and its web3/eth-account pins. ``format_eip4361``
matches that reference implementation byte for byte (MangroveAI pins the same text in
``tests/v402/test_siwx.py``), so the signature verifies on either side.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import httpx

SIWX_HEADER = "SIGN-IN-WITH-X"
#: Fixed text. The wallet's signing guard refuses any other statement, so this helper
#: cannot be turned into a way to sign arbitrary wording.
STATEMENT = "Sign in to MangroveAI"
VERSION = "1"
#: How long a proof stays valid. MangroveAI refuses one older than five minutes and
#: spends each nonce once, so a proof is built per call, not reused.
WINDOW_SECONDS = 300
#: How far back a proof's issue time is stamped. MangroveAI's verifier refuses an issue
#: time even slightly in the future, so a client clock running a few seconds fast would
#: otherwise be refused at random. Within the wallet guard's 60-second tolerance.
CLOCK_SKEW_SECONDS = 30


def format_eip4361(*, domain, address, uri, version, chain_id, nonce, issued_at,
                   statement=None, expiration_time=None, not_before=None, request_id=None,
                   resources=None) -> str:
    """The EIP-4361 message text, field for field (identical to MangroveAI's)."""
    lines = [f"{domain} wants you to sign in with your Ethereum account:", address, ""]
    # EIP-4361 grammar: address LF LF [statement LF] LF "URI: " -- so a message with
    # no statement still carries the blank line the statement would have closed.
    lines += [statement, ""] if statement else [""]
    lines += [f"URI: {uri}", f"Version: {version}", f"Chain ID: {chain_id}",
              f"Nonce: {nonce}", f"Issued At: {issued_at}"]
    if expiration_time:
        lines.append(f"Expiration Time: {expiration_time}")
    if not_before:
        lines.append(f"Not Before: {not_before}")
    if request_id is not None:
        lines.append(f"Request ID: {request_id}")
    if resources:
        lines.append("Resources:")
        lines += [f"- {resource}" for resource in resources]
    return "\n".join(lines)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_siwx_header(endpoint: str) -> str:
    """A fresh, signed ``SIGN-IN-WITH-X`` value for one request to ``endpoint``.

    Signed by the configured payer wallet (the wallet that pays in this mode, so the
    identity a proof gives and the one a payment gives are the same), on the configured
    network. Raises when there is no payer wallet or network to sign with.
    """
    from x402.extensions.sign_in_with_x import encode_siwx_header
    from x402.extensions.sign_in_with_x.types import SIWxPayload

    from src.services import wallet_manager, x402_payer

    payer = x402_payer.resolve_payer_wallet(None)
    network = x402_payer._require_network()
    url = httpx.URL(endpoint)
    domain = url.netloc.decode("ascii")
    issued = datetime.now(timezone.utc) - timedelta(seconds=CLOCK_SKEW_SECONDS)
    fields = {
        "domain": domain, "address": payer, "uri": str(url), "version": VERSION,
        "chainId": network, "type": "eip191", "nonce": secrets.token_hex(16),
        "issuedAt": _stamp(issued),
        "expirationTime": _stamp(issued + timedelta(seconds=WINDOW_SECONDS)),
        "statement": STATEMENT,
    }
    signature = wallet_manager.sign_siwx_message(
        wallet_address=payer, domain=domain, uri=fields["uri"], chain=network,
        nonce=fields["nonce"], issued_at=fields["issuedAt"],
        expiration_time=fields["expirationTime"])
    return encode_siwx_header(SIWxPayload(**fields, signature=signature))
