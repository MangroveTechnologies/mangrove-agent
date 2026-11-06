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
  When ``eth_account`` is not importable, as under the system ``python3`` that
  runs the Claude hook, the identical repository copy
  ``.claude/hooks/bip39-english.txt`` is read instead.
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
   24-word window of BIP-39 words with a valid BIP-39 checksum. Words are the
   letter runs of the text, so a phrase separated by spaces, commas, hyphens,
   newlines or list numbers (``1. word 2. word``) is found. The checksum
   keeps strategy prose made of wordlist words (``price above average``) from
   being refused; such a run still has about a 1 in 16 chance per 12-word
   window of passing it.
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
and walked. Other text that names a key field (quoted with ``"`` or ``'``) is
withheld when the field's value is key-shaped, when a ``0x`` key appears
anywhere in it without a hash label, or when it also holds a 12-word run or
an XRPL seed.

Client hooks (``scan_hook_content``): ``scripts/wallet_secret_guard.py``
adapts this module to Claude Code and Codex hook events. Hook content (prompts,
shell and file tool input, stdout, stderr, errors) is not a structured MCP
payload, so it gets a strict, shape-preserving scan with the same secret
shapes and wordlist: every string and number is checked with ``find_in_text``
(any 64-hex value, any 12-word BIP-39 run, any XRPL seed), and dictionary keys
are checked too. A string that parses as JSON (duplicate keys refused) is
scanned parsed and again as raw text with exempted values removed, so a parser
difference fails closed. A key-named ancestor removes every exemption. The
only exemptions are:

- an exact HTTPS ``/tx/0x...`` link with no query or fragment on an
  allowlisted explorer host, in a ``url``-like field;
- the single hash argument of a strict JSON-RPC 2.0 ``eth_getTransactionByHash``
  or ``eth_getTransactionReceipt`` request, only below a dictionary whose
  ``url`` or ``endpoint`` field is an allowlisted RPC host;
- tool output and errors: a line holding only a hash label and one 64-hex
  value (``Transaction hash: 0x...``), with spaces or tabs only;
- structured MCP tool results: a 64-hex value that is the whole value of a
  public-hash field (``tx_hash``, ``block_hash``, ``sha256`` and similar).

Label words in free text never exempt a value in hook content.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
_QUOTED_FIELD_RE = re.compile(r"""["']([A-Za-z0-9_\-]{1,64})["']\s*[:=]""")
_VALUE_PREFIX_RE = re.compile(r"""[\s"'{\[]*""")
_PREFIXED_KEY_RE = re.compile(r"(?<![0-9A-Za-z])0[xX][0-9a-fA-F]{64}(?![0-9A-Za-z])")
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


_REPOSITORY_WORDLIST = Path(__file__).resolve().parents[3] / ".claude" / "hooks" / "bip39-english.txt"


def _wordlist_text() -> str:
    try:
        return (resources.files("eth_account.hdaccount") / "wordlist" / "english.txt").read_text("utf-8")
    except ModuleNotFoundError:
        return _REPOSITORY_WORDLIST.read_text("utf-8")


@lru_cache(maxsize=1)
def _bip39_list() -> tuple[str, ...]:
    words = tuple(word.strip() for word in _wordlist_text().splitlines() if word.strip())
    if len(words) != 2048:
        raise ValueError("BIP-39 wordlist must hold 2048 words")
    return words


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


def has_valid_mnemonic(text: str) -> bool:
    """A 12, 15, 18, 21 or 24-word window of BIP-39 words with a valid checksum.

    Words are the letter runs of the lowercased text, so spaces, commas,
    hyphens, newlines and list numbers all separate words without ending a run.
    """
    for run in _word_runs(text):
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
    """Non-JSON text that names a key field (quoted with ``"`` or ``'``).

    Refused when the value after the field name, past whitespace, quotes and
    opening ``{`` or ``[``, is key-shaped (with or without ``0x``); when a
    ``0x`` key appears anywhere in the text and is not labelled as a hash; or
    when the text also holds a 12-word run or an XRPL seed.
    """
    named = False
    for match in _QUOTED_FIELD_RE.finditer(text):
        if not is_key_field(match.group(1), arguments=arguments):
            continue
        named = True
        rest = text[match.end():]
        prefix = _VALUE_PREFIX_RE.match(rest)
        if _HEX_KEY_RE.match(rest, prefix.end() if prefix else 0):
            return EVM_KEY
    if not named:
        return None
    for match in _PREFIXED_KEY_RE.finditer(text):
        if not _hash_labelled(text, match.start()):
            return EVM_KEY
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
    hashes. JSON carried as text content is parsed and walked, and the raw
    text is then scanned with ``_contextual_text_hit`` as well, so a parser
    that drops duplicate keys cannot hide a key-named value.
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
                hit = walk(nested, None)
                if hit:
                    return hit
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


# --- Client hook content (strict) --------------------------------------------

HOOK_REDACTED = "[wallet-secret content withheld]"
HOOK_MAX_DEPTH = 32
HOOK_MAX_NODES = 100_000
_HOOK_PUBLIC_HASH_FIELDS = frozenset({
    "txhash", "transactionhash", "blockhash", "sha256", "digest", "contractdigest",
    "codehash", "requesthash",
})
_HOOK_PUBLIC_URL_FIELDS = frozenset({"url", "uri", "refid", "targeturl"})
_HOOK_RPC_URL_FIELDS = frozenset({"url", "uri", "endpoint", "rpcurl", "targeturl"})
_EXPLORER_HOSTS = frozenset({
    "basescan.org", "sepolia.basescan.org", "etherscan.io", "sepolia.etherscan.io",
    "arbiscan.io", "sepolia.arbiscan.io",
})
# The public RPC endpoints the agent's configuration uses.
_RPC_HOSTS = frozenset({"mainnet.base.org", "sepolia.base.org"})
# Never equal to a normalized field name, which holds only [a-z0-9].
_RPC_HASH_PARAM = "<json-rpc transaction hash>"
_TRANSACTION_READ_METHODS = frozenset({"eth_getTransactionByHash", "eth_getTransactionReceipt"})
_PUBLIC_HASH_LINE = re.compile(
    r"(?im)^[ \t]*(?:tx[_ ]?hash|transaction[_ ]?hash|block[_ ]?hash|sha256|digest)[ \t]*[:=][ \t]*"
    r"((?:0[xX])?[a-fA-F0-9]{64})[ \t]*\r?$")


def _exact_https_host(value: str, hosts: frozenset[str]) -> bool:
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.hostname in hosts and url.netloc == url.hostname
                and not url.query and not url.fragment)
    except ValueError:
        return False


def public_transaction_url(value: str) -> bool:
    """An exact HTTPS transaction link on an allowlisted explorer host."""
    return (_exact_https_host(value, _EXPLORER_HOSTS)
            and re.fullmatch(r"/tx/0x[a-fA-F0-9]{64}/?", urlsplit(value).path) is not None)


def _rpc_endpoint(value: dict) -> bool:
    """The dict names an allowlisted RPC host in a URL-like field."""
    return any(normalize_field(str(key)) in _HOOK_RPC_URL_FIELDS and isinstance(item, str)
               and _exact_https_host(item, _RPC_HOSTS) and urlsplit(item).path in {"", "/"}
               for key, item in value.items())


def transaction_read_request(value: dict) -> bool:
    """The exact JSON-RPC 2.0 envelope of a single-hash transaction read."""
    params = value.get("params")
    return (value.get("jsonrpc") == "2.0"
            and isinstance(value.get("method"), str) and value["method"] in _TRANSACTION_READ_METHODS
            and set(value) <= {"jsonrpc", "method", "params", "id"}
            and isinstance(params, list) and len(params) == 1 and isinstance(params[0], str)
            and re.fullmatch(r"0x[a-fA-F0-9]{64}", params[0]) is not None)


def _unique_object(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate JSON object key")
    return dict(pairs)


def _decode_json(text: str) -> Any:
    """Parse JSON text, refusing duplicate keys so the parsed and raw views agree."""
    try:
        return json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return None


def scan_hook_content(value: Any, *, output: bool, structured: bool = False) -> tuple[Any, bool]:
    """Strict scan of client hook content; returns (shape-preserving redaction, found).

    ``output`` is True for tool output and errors, which allows labelled hash
    lines. ``structured`` is True for an MCP tool result, which also allows
    whole-value public-hash fields; shell and file output never get that
    exemption. A JSON-RPC transaction read is exempt only below a dict that
    names an allowlisted RPC host. A string that parses as JSON is scanned
    both parsed and as raw text with exempted values removed, so a parser
    difference fails closed. Raises ``ValueError`` past the depth or node limit.
    """
    nodes = 0

    def scan(item: Any, secret: bool, field: str, depth: int, rpc_ok: bool,
             exempt: list[str]) -> tuple[Any, bool]:
        nonlocal nodes
        nodes += 1
        if depth > HOOK_MAX_DEPTH or nodes > HOOK_MAX_NODES:
            raise ValueError("Detector traversal limit")
        if isinstance(item, dict):
            result, hit = {}, False
            rpc_ok = rpc_ok or (not secret and _rpc_endpoint(item))
            rpc_read = rpc_ok and not secret and transaction_read_request(item)
            for key, child in item.items():
                clean_key, key_hit = scan(str(key), False, "", depth + 1, False, exempt)
                name = normalize_field(str(key))
                child_field = _RPC_HASH_PARAM if rpc_read and key == "params" else name
                clean, found = scan(child, secret or is_key_field(str(key), arguments=True),
                                    child_field, depth + 1, rpc_ok, exempt)
                result[clean_key] = clean
                hit = hit or found or key_hit
            return result, hit
        if isinstance(item, (list, tuple)):
            result_list, hit = [], False
            for child in item:
                clean, found = scan(child, secret, field, depth + 1, rpc_ok, exempt)
                result_list.append(clean)
                hit = hit or found
            return result_list, hit
        if isinstance(item, bool) or item is None:
            return item, False
        if isinstance(item, (int, float)):
            return (HOOK_REDACTED, True) if find_in_text(str(item)) else (item, False)
        if not isinstance(item, str):
            return item, False
        decoded = _decode_json(item)
        if isinstance(decoded, (dict, list, str)) and decoded != item:
            nested_exempt: list[str] = []
            clean, hit = scan(decoded, secret, field, depth + 1, rpc_ok, nested_exempt)
            if not hit:
                raw = item
                for value_text in nested_exempt:
                    raw = raw.replace(json.dumps(value_text)[1:-1], " ")
                hit = find_in_text(raw) is not None
                clean = HOOK_REDACTED if hit else clean
            if hit:
                return (json.dumps(clean, ensure_ascii=True) if clean != HOOK_REDACTED else clean), True
            exempt.extend(nested_exempt)
            return item, False
        candidate = item
        if not secret:
            if field == _RPC_HASH_PARAM and re.fullmatch(r"0x[a-fA-F0-9]{64}", item):
                exempt.append(item)
                return item, False
            if field in _HOOK_PUBLIC_URL_FIELDS and public_transaction_url(item):
                exempt.append(item)
                return item, False
            if structured and field in _HOOK_PUBLIC_HASH_FIELDS and _HEX_KEY_RE.fullmatch(item.strip()):
                exempt.append(item)
                return item, False
            if output:
                candidate = _PUBLIC_HASH_LINE.sub("[public hash]", candidate)
                if candidate != item:
                    exempt.append(item)
        if find_in_text(candidate):
            return HOOK_REDACTED, True
        return item, False

    return scan(value, False, "", 0, False, [])
