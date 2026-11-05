"""Detect wallet secrets in MCP tool arguments and results.

This is the server-side counterpart of ``.claude/hooks/block-wallet-secrets.sh``.
That hook only runs inside Claude Code. Every MCP client (Claude Code, the
command client ``scripts/local_mcp_bridge.py`` used by Codex, Grok, Grok Bot
and Perplexity, the restricted launcher, and any remote MCP client) reaches
the agent through ``PricedFastMCP.call_tool``, which applies these checks.

The shapes match the hook: an EVM private key (``0x`` + 64 hex), a run of at
least 12 consecutive BIP-39 English words, and an XRPL family seed. The
wordlist comes from ``eth_account`` (a runtime dependency, present in the
Docker image), and a unit test keeps it identical to the hook's
``bip39-english.txt``.

Transaction hashes have the same shape as an EVM key, so a bare ``0x`` + 64 hex
value is a secret only in a key-named field. Mnemonics and XRPL seeds have no
legitimate use in tool arguments and are refused anywhere in them.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

EVM_KEY = "evm_private_key"
MNEMONIC = "bip39_mnemonic"
XRPL_SEED = "xrpl_family_seed"

LABELS = {
    EVM_KEY: "an EVM private key",
    MNEMONIC: "a BIP-39 recovery phrase",
    XRPL_SEED: "an XRPL family seed",
}

_EVM_KEY_RE = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_XRPL_SEED_RE = re.compile(
    r"(?<![a-zA-Z0-9])s(?:Ed[1-9A-HJ-NP-Za-km-z]{28}|[1-9A-HJ-NP-Za-km-z]{28})(?![a-zA-Z0-9])"
)
_WORD_RE = re.compile(r"[a-z]+")
_MNEMONIC_RUN = 12

# Field names that carry key material (same list as the Claude hook).
KEY_FIELDS = frozenset({
    "private_key", "seed", "seed_phrase", "secret", "mnemonic",
    "wallet_secret", "master_key", "key",
})
# A vault token is a short random handle, never key-shaped, so a key-shaped
# vault_token argument is a pasted secret.
_ARGUMENT_KEY_FIELDS = KEY_FIELDS | {"vault_token"}
_KEY_FIELD_TEXT_RE = re.compile(
    r'"(?:' + "|".join(sorted(KEY_FIELDS)) + r')"\s*:'
)


@lru_cache(maxsize=1)
def bip39_words() -> frozenset[str]:
    text = (resources.files("eth_account.hdaccount") / "wordlist" / "english.txt").read_text("utf-8")
    return frozenset(word.strip() for word in text.splitlines() if word.strip())


def has_mnemonic(text: str) -> bool:
    words = bip39_words()
    run = 0
    for token in _WORD_RE.findall(text.lower()):
        run = run + 1 if token in words else 0
        if run >= _MNEMONIC_RUN:
            return True
    return False


def find_in_text(text: str) -> str | None:
    """Strict scan of free text: any secret shape anywhere (hook user mode)."""
    if _XRPL_SEED_RE.search(text):
        return XRPL_SEED
    if _EVM_KEY_RE.search(text):
        return EVM_KEY
    if has_mnemonic(text):
        return MNEMONIC
    return None


def _key_value_hit(value: Any) -> str | None:
    """A key-named field holding any secret shape."""
    if isinstance(value, str):
        return find_in_text(value)
    if isinstance(value, (dict, list, tuple)):
        return find_in_text(json.dumps(value, default=str))
    return None


def _parse_json_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _contextual_text_hit(text: str) -> str | None:
    """Hook tool-mode fallback for non-JSON text: a key field name plus a key shape."""
    if not _KEY_FIELD_TEXT_RE.search(text):
        return None
    if _EVM_KEY_RE.search(text):
        return EVM_KEY
    if has_mnemonic(text):
        return MNEMONIC
    if _XRPL_SEED_RE.search(text):
        return XRPL_SEED
    return None


def find_in_arguments(arguments: Any) -> str | None:
    """Return the secret kind found in MCP tool arguments, or None.

    Refuses a mnemonic or XRPL seed anywhere, and an EVM key in a key-named
    field (including ``vault_token``). A transaction hash in a field such as
    ``tx_hash`` is allowed.
    """

    def walk(value: Any, field: str | None) -> str | None:
        if field is not None and field.lower() in _ARGUMENT_KEY_FIELDS:
            hit = _key_value_hit(value)
            if hit:
                return hit
        if isinstance(value, dict):
            for key, item in value.items():
                hit = walk(item, str(key))
                if hit:
                    return hit
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                hit = walk(item, field)
                if hit:
                    return hit
            return None
        if isinstance(value, str):
            if _XRPL_SEED_RE.search(value):
                return XRPL_SEED
            if has_mnemonic(value):
                return MNEMONIC
            nested = _parse_json_text(value)
            if nested is not None:
                return walk(nested, None)
            return _contextual_text_hit(value)
        return None

    return walk(arguments, None)


def find_in_result(result: Any) -> str | None:
    """Return the secret kind found in a tool result, or None.

    Only key-named fields count (hook tool mode), because results routinely
    carry transaction hashes. JSON carried as text content is parsed and
    walked; other text falls back to the hook's field-name-plus-shape match.
    """

    def walk(value: Any, field: str | None) -> str | None:
        if field is not None and field.lower() in KEY_FIELDS:
            hit = _key_value_hit(value)
            if hit:
                return hit
        if isinstance(value, dict):
            for key, item in value.items():
                hit = walk(item, str(key))
                if hit:
                    return hit
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                hit = walk(item, None)
                if hit:
                    return hit
            return None
        if isinstance(value, str):
            nested = _parse_json_text(value)
            if nested is not None:
                return walk(nested, None)
            return _contextual_text_hit(value)
        return None

    return walk(_jsonable(result), None)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - fall back to the raw object
            return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
