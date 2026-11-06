"""Detect wallet secrets in MCP tool arguments and results.

This is the server-side counterpart of ``.claude/hooks/block-wallet-secrets.sh``.
That hook only runs inside Claude Code. Every MCP client (Claude Code, the
command client ``scripts/local_mcp_bridge.py`` used by Codex, Grok, Grok Bot
and Perplexity, the restricted launcher, and any remote MCP client) reaches
the agent through ``PricedFastMCP.call_tool``, which applies these checks.

Secret shapes:

- EVM private key: 64 hex characters, with or without a ``0x`` or ``0X``
  prefix. ``wallet_manager.import_wallet`` accepts both forms, and the bare
  form is what MetaMask's "Show private key" displays.
- BIP-39 recovery phrase: consecutive words from the English BIP-39 list,
  read from ``eth_account`` (a runtime dependency, present in the Docker image).
- XRPL family seed: ``s`` followed by 28 base58 characters (``sEd`` for Ed25519).

Field names are normalized before matching: lowercased, with every character
other than a letter or digit removed, so ``privateKey``, ``PRIVATE_KEY``,
``private-key`` and ``private_key`` are the same field. A field is key-named
when its normalized name is in ``KEY_FIELD_NAMES`` or contains one of
``KEY_FIELD_MARKERS``.

Arguments (``find_in_arguments``):

1. A key-named field, or ``vault_token``, is refused when its value holds any
   secret shape: 64 hex characters with or without ``0x``, a run of 12 or more
   BIP-39 words (no checksum needed), or an XRPL seed. A vault token is a
   22-character URL-safe handle, so a key-shaped one is a pasted secret.
2. Any string is refused when it holds an XRPL seed, or a 12, 15, 18, 21 or
   24-word window of BIP-39 words with a valid BIP-39 checksum. Words count
   as consecutive when only whitespace or list numbers (``1.``, ``2)``)
   separate them; other punctuation ends a run. The checksum and the
   punctuation rule keep strategy prose made of wordlist words
   (``price above average, volume above average``) from being refused. An
   unpunctuated run of wordlist words still has about a 1 in 16 chance per
   12-word window of passing the checksum. A phrase written with commas
   between its words is not detected here; it is detected in a key-named
   field (rule 1).
3. Any other string is refused when it holds 64 hex characters, with or
   without ``0x``, embedded in text (``"import my wallet 0x..."``) or anywhere
   in a free-text field (``PROSE_FIELD_NAMES``, such as ``query`` or
   ``description``). Because 32-byte identifiers have the same shape, a value
   passes when its field is named for a hash or identifier (the normalized
   name contains ``hash``, ``digest`` or ``txid``, or ends in ``id``), when it
   is the whole value of a field that is not free text (a transaction hash,
   definition or config hash, order id or sha256 digest), or when one of the
   three words before it labels it as a hash (``tx``, ``transaction``,
   ``hash``, ``block``, ``digest``, ``order`` and similar, ``_HASH_WORDS``).

Results (``find_in_result``): only key-named fields count, because results
routinely carry transaction hashes. JSON carried as text content is parsed
and walked. Other text is withheld when a quoted key-named field name is
followed directly by a key-shaped value, or when it appears in the same text
as a 12-word run or an XRPL seed.

Differences from the Claude hook: the hook matches only ``0x`` keys and exact
field names, and treats any 12-word run as a phrase. ``find_in_text`` is the
strict scan used for key-named values; it agrees with the hook's user mode on
``0x`` keys, phrases and seeds, and also matches bare hex keys.
"""
from __future__ import annotations

import hashlib
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

_HEX_KEY_RE = re.compile(r"(?<![0-9A-Za-z])(?:0[xX])?[0-9a-fA-F]{64}(?![0-9A-Za-z])")
_XRPL_SEED_RE = re.compile(
    r"(?<![a-zA-Z0-9])s(?:Ed[1-9A-HJ-NP-Za-km-z]{28}|[1-9A-HJ-NP-Za-km-z]{28})(?![a-zA-Z0-9])"
)
_WORD_RE = re.compile(r"[a-z]+")
_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
_NORMALIZE_RE = re.compile(r"[^a-z0-9]")
_QUOTED_FIELD_RE = re.compile(r'"([A-Za-z0-9_\-]{1,64})"\s*:\s*')
_LIST_NUMBER_RE = re.compile(r"\d{1,2}[.):]?")
_PHRASE_CHUNK_RE = re.compile(r"([\"'(\[]*)([a-z]+)([.,;:!?\"')\]]*)")
_MNEMONIC_RUN = 12
_MNEMONIC_LENGTHS = (12, 15, 18, 21, 24)

# Normalized field names that carry key material.
KEY_FIELD_NAMES = frozenset({
    "privatekey", "privkey", "pk", "secretkey", "seed", "seedphrase", "secret",
    "mnemonic", "mnemonicphrase", "recoveryphrase", "secretrecoveryphrase",
    "walletsecret", "masterkey", "key", "signingkey", "walletkey",
})
# A normalized field name containing one of these is also key-named,
# for example ``evm_private_key`` or ``walletMnemonic``.
KEY_FIELD_MARKERS = (
    "privatekey", "privkey", "secretkey", "seedphrase", "mnemonic",
    "recoveryphrase", "walletsecret", "masterkey", "signingkey",
)
# Key-named in tool arguments only: import_wallet takes the vault token there.
_ARGUMENT_KEY_FIELDS = frozenset({"vaulttoken"})
# Normalized free-text argument fields; a 64-hex value here is never an identifier.
PROSE_FIELD_NAMES = frozenset({
    "query", "q", "xquery", "description", "text", "message", "prompt", "note",
    "notes", "content", "question", "userintent", "why", "term", "topic", "input",
})
# Normalized words that label the 64-hex value after them as a public hash.
_HASH_WORDS = frozenset({
    "tx", "txn", "txid", "txhash", "transaction", "transactionhash", "hash",
    "blockhash", "block", "digest", "sha256", "checksum", "receipt", "order",
    "orderid", "orderhash", "id", "definitionhash", "confighash",
})
_HASH_CONTEXT_WORDS = 3


def normalize_field(name: str) -> str:
    return _NORMALIZE_RE.sub("", name.lower())


def is_key_field(name: str | None, *, arguments: bool = False) -> bool:
    """True when a field name, after normalization, names key material."""
    if name is None:
        return False
    norm = normalize_field(name)
    if norm in KEY_FIELD_NAMES or (arguments and norm in _ARGUMENT_KEY_FIELDS):
        return True
    return any(marker in norm for marker in KEY_FIELD_MARKERS)


def _is_identifier_field(norm: str) -> bool:
    return "hash" in norm or "digest" in norm or "txid" in norm or norm.endswith("id")


@lru_cache(maxsize=1)
def _bip39_list() -> tuple[str, ...]:
    text = (resources.files("eth_account.hdaccount") / "wordlist" / "english.txt").read_text("utf-8")
    return tuple(word.strip() for word in text.splitlines() if word.strip())


@lru_cache(maxsize=1)
def bip39_words() -> frozenset[str]:
    return frozenset(_bip39_list())


@lru_cache(maxsize=1)
def _bip39_index() -> dict[str, int]:
    return {word: position for position, word in enumerate(_bip39_list())}


def bip39_checksum_valid(words: list[str] | tuple[str, ...]) -> bool:
    """True when ``words`` is an English BIP-39 phrase of 12-24 words with a valid checksum."""
    count = len(words)
    if count not in _MNEMONIC_LENGTHS:
        return False
    index = _bip39_index()
    value = 0
    for word in words:
        position = index.get(word)
        if position is None:
            return False
        value = (value << 11) | position
    checksum_bits = count // 3
    entropy = (value >> checksum_bits).to_bytes((count * 11 - checksum_bits) // 8, "big")
    expected = hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits)
    return expected == value & ((1 << checksum_bits) - 1)


def _word_runs(text: str) -> list[list[str]]:
    """Maximal runs of consecutive BIP-39 words in ``text``."""
    words = bip39_words()
    runs: list[list[str]] = []
    run: list[str] = []
    for token in _WORD_RE.findall(text.lower()):
        if token in words:
            run.append(token)
            continue
        if run:
            runs.append(run)
        run = []
    if run:
        runs.append(run)
    return runs


def has_mnemonic(text: str) -> bool:
    """A run of at least 12 BIP-39 words; no checksum (the hook's rule)."""
    return any(len(run) >= _MNEMONIC_RUN for run in _word_runs(text))


def _phrase_runs(text: str) -> list[list[str]]:
    """Runs of whitespace-separated BIP-39 words, broken by punctuation.

    A list number such as ``1.`` or ``12)`` neither joins nor breaks a run. A
    word with leading punctuation starts a new run, and a word with trailing
    punctuation ends one, so ``"... above average, volume ..."`` is two runs.
    """
    words = bip39_words()
    runs: list[list[str]] = []
    run: list[str] = []

    def close() -> None:
        nonlocal run
        if run:
            runs.append(run)
        run = []

    for chunk in text.lower().split():
        if _LIST_NUMBER_RE.fullmatch(chunk):
            continue
        match = _PHRASE_CHUNK_RE.fullmatch(chunk)
        if not match or match.group(2) not in words:
            close()
            continue
        if match.group(1):
            close()
        run.append(match.group(2))
        if match.group(3):
            close()
    close()
    return runs


def has_valid_mnemonic(text: str) -> bool:
    """A 12, 15, 18, 21 or 24-word window of BIP-39 words with a valid checksum."""
    for run in _phrase_runs(text):
        for length in _MNEMONIC_LENGTHS:
            for start in range(len(run) - length + 1):
                if bip39_checksum_valid(run[start:start + length]):
                    return True
    return False


def find_in_text(text: str) -> str | None:
    """Strict scan of a value that should hold no key material: any secret shape."""
    if _XRPL_SEED_RE.search(text):
        return XRPL_SEED
    if _HEX_KEY_RE.search(text):
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


def _hash_labelled(text: str, start: int) -> bool:
    before = _NAME_RE.findall(text[:start])[-_HASH_CONTEXT_WORDS:]
    return any(normalize_field(word) in _HASH_WORDS for word in before)


def _free_text_hex_hit(text: str, field: str | None) -> str | None:
    """A 64-hex value in a non-key argument string that is not an identifier."""
    norm = normalize_field(field) if field is not None else ""
    if norm and _is_identifier_field(norm):
        return None
    prose = norm in PROSE_FIELD_NAMES
    for match in _HEX_KEY_RE.finditer(text):
        if not prose and text.strip() == match.group(0):
            continue
        if _hash_labelled(text, match.start()):
            continue
        return EVM_KEY
    return None


def _contextual_text_hit(text: str, *, arguments: bool) -> str | None:
    """Non-JSON text with a quoted key-named field: a key directly after the
    field name, or a 12-word run or XRPL seed anywhere in the same text."""
    named = False
    for match in _QUOTED_FIELD_RE.finditer(text):
        if not is_key_field(match.group(1), arguments=arguments):
            continue
        named = True
        if _HEX_KEY_RE.match(text[match.end():].lstrip('"')):
            return EVM_KEY
    if not named:
        return None
    if _XRPL_SEED_RE.search(text):
        return XRPL_SEED
    if has_mnemonic(text):
        return MNEMONIC
    return None


def find_in_arguments(arguments: Any) -> str | None:
    """Return the secret kind found in MCP tool arguments, or None.

    The rules are in the module docstring. Transaction hashes in hash-named
    fields, as whole field values, or labelled as hashes in text pass.
    """

    def walk(value: Any, field: str | None) -> str | None:
        if is_key_field(field, arguments=True):
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
            if has_valid_mnemonic(value):
                return MNEMONIC
            nested = _parse_json_text(value)
            if nested is not None:
                return walk(nested, None)
            return _free_text_hex_hit(value, field) or _contextual_text_hit(value, arguments=True)
        return None

    return walk(arguments, None)


def find_in_result(result: Any) -> str | None:
    """Return the secret kind found in a tool result, or None.

    Only key-named fields count, because results routinely carry transaction
    hashes. JSON carried as text content is parsed and walked; other text
    falls back to ``_contextual_text_hit``.
    """

    def walk(value: Any, field: str | None) -> str | None:
        if is_key_field(field):
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
            return _contextual_text_hit(value, arguments=False)
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
