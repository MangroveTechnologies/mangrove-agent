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
- EVM private key as an integer: a JSON integer, or a run of 58 to 78 decimal
  digits, whose value is a valid secp256k1 scalar of at least 2**192. Token
  amounts stay far below that bound (10**30 is 1e12 tokens at 18 decimals),
  and an unlimited approval (2**256 - 1) is above the curve order.

Field names are normalized before matching: lowercased, with every character
other than a letter or digit removed, so ``privateKey``, ``PRIVATE_KEY``,
``private-key`` and ``private_key`` are the same field. A field is key-named
when its normalized name is in ``KEY_FIELD_NAMES``, contains one of
``KEY_FIELD_MARKERS``, or has a name segment in ``KEY_FIELD_SEGMENTS``
(``order_seed``, ``seedHex``). Segments are the snake, kebab and camel case
parts of the name.

Arguments (``find_in_arguments``):

1. A key-named field, or ``vault_token``, is refused when its value holds any
   secret shape: 64 hex characters with or without ``0x``, a run of 12 or more
   BIP-39 words (no checksum needed), an XRPL seed, or an integer key. A vault
   token is a 22-character URL-safe handle, so a key-shaped one is a pasted
   secret.
2. Any string is refused when it holds an XRPL seed or a recovery phrase
   (``has_recovery_phrase``): a window of 12, 15, 18, 21 or 24 consecutive
   BIP-39 words that has a valid BIP-39 checksum, or that is not in
   alphabetical order and repeats no word (12 or 15 words) or at most one
   word (18 to 24 words). A phrase with a mistyped word or a bad checksum is
   therefore still refused, and a checksum-valid phrase is refused whatever
   its order or repeats. Words are the letter runs of the text, so a phrase
   separated by spaces, commas, hyphens, newlines or list numbers (``1. word
   2. word``) is found, also with other wordlist words around it. Words
   joined by an underscore or a case change (``word_word``, ``wordWord``) or
   written with no separator also form runs, but a window that crosses such a
   join counts only with a valid checksum and at most one repeated word in
   four, so ``snake_case`` and ``camelCase`` identifiers do not count. Text
   that is not ASCII is NFKC-normalized and read with invisible characters
   both removed and as spaces. The string values of the arguments are also
   joined in order and scanned the same way, which finds a phrase passed as
   a list of words or split across fields. Strategy prose made of
   wordlist words repeats them (``price above average, volume above
   average``), so it counts only when one of its windows happens to pass the
   checksum (about 1 in 16 per 12-word window). An excerpt of the wordlist
   itself (consecutive wordlist entries) is not counted. A phrase with a wrong
   word is missed only when it also repeats a word: about 3% of 12-word and
   5% of 15-word phrases, under 1% from 18 words. One call evaluates at most
   ``_WINDOW_BUDGET`` checksums; text needing more is refused as a possible
   phrase, which keeps the scan linear in the input size.
3. Any other string is refused when it holds 64 hex characters, with or
   without ``0x``, embedded in text (``"import my wallet 0x..."``) or anywhere
   in a free-text field (``PROSE_FIELD_NAMES``, such as ``query`` or
   ``description``). Because 32-byte identifiers have the same shape, a value
   passes when its field is named for a hash or identifier (a name segment in
   ``_IDENTIFIER_SEGMENTS`` or a normalized name in ``_HASH_WORDS``, and no
   key-named part), when it is the whole value of a field that is not free
   text (a transaction hash, definition or config hash, order id or sha256
   digest), or when one of the three words before it labels it as a hash
   (``tx``, ``transaction``, ``hash``, ``block``, ``digest``, ``order`` and
   similar, ``_HASH_WORDS``) and none of them is a key word such as ``key``
   or ``seed``. Integer keys follow the same rules, except that a whole
   decimal value is refused unless its field is an identifier.
4. A string that parses as JSON is walked as a structure and also scanned as
   raw text: a key-named field whose raw value is a secret shape, which the
   parser can hide behind a later duplicate key, and any 64-hex or integer key
   in the text that is not one of the parsed string values (a key used as an
   object key, for example), unless it is labelled as a hash.

Results (``find_in_result``): only key-named fields count, because results
routinely carry transaction hashes. JSON carried as text content is parsed
and walked, and its raw text is then scanned like any other text. Text that
names a key field (quoted with ``"`` or ``'``) is withheld when the field's
raw value is a secret shape, when a ``0x`` key appears anywhere in it without
a hash label and is not the value of a field named exactly as a public hash
(``approval_tx``, ``tx_hash``; ``_ATTRIBUTED_HASH_FIELDS``), or when it also
holds an XRPL seed or a recovery phrase (rule 2).

Errors (``redact_error``): an error message has every argument value it
repeats removed, and is withheld when what remains holds a secret shape.

Differences from the Claude hook: the hook matches only ``0x`` keys and exact
field names, and treats any 12-word run as a phrase. ``find_in_text`` is the
strict scan used for key-named values; it agrees with the hook's user mode on
``0x`` keys, phrases and seeds, and also matches bare hex keys.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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
_DECIMAL_KEY_RE = re.compile(r"(?<![0-9A-Za-z.])[0-9]{58,78}(?![0-9A-Za-z.])")
_XRPL_SEED_RE = re.compile(
    r"(?<![a-zA-Z0-9])s(?:Ed[1-9A-HJ-NP-Za-km-z]{28}|[1-9A-HJ-NP-Za-km-z]{28})(?![a-zA-Z0-9])"
)
_WORD_RE = re.compile(r"[a-z]+")
# Letter runs, split at lower-to-upper case changes (``inputToken`` is two words).
_CASED_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
# Characters that render as nothing; a phrase may carry them inside or between words.
_INVISIBLE_RE = re.compile("[\u00ad\u180e\u200b-\u200f\u2060-\u2064\ufeff]")
_LETTERS_RE = re.compile(r"[A-Za-z]+")
_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
_NORMALIZE_RE = re.compile(r"[^a-z0-9]")
_SEGMENT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_QUOTED_FIELD_RE = re.compile(r"""["']([A-Za-z0-9_\-]{1,64})["']\s*[:=]""")
_VALUE_PREFIX_RE = re.compile(r"""[\s"'{\[]*""")
_PREFIXED_KEY_RE = re.compile(r"(?<![0-9A-Za-z])0[xX][0-9a-fA-F]{64}(?![0-9A-Za-z])")
_ATTRIBUTED_RE = re.compile(r"""["']([A-Za-z0-9_\-]{1,64})["']\s*[:=]\s*["']?\s*$""")
_MNEMONIC_RUN = 12
_MNEMONIC_LENGTHS = (12, 15, 18, 21, 24)
_MNEMONIC_LENGTH_SET = frozenset(_MNEMONIC_LENGTHS)
# Repeated words a recovery-phrase window may hold without a valid checksum,
# for 12-15 and for 18-24 words.
_PLAIN_REPEATS_SHORT, _PLAIN_REPEATS_LONG = 0, 1
# secp256k1 group order; a private key is an integer in [1, n).
_SECP256K1_N = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
# Smallest integer treated as a key. A random key is below it with probability 2**-64.
_MIN_INT_KEY = 1 << 192
# Characters read before a match for hash labels, and before a 0x value for its field name.
_LOOKBACK_CHARS = 256
_ATTRIBUTION_CHARS = 80
# Checksums one scan may evaluate before refusing the text as a possible phrase.
_WINDOW_BUDGET = 50_000
# Longest error message scanned and returned by redact_error.
_ERROR_CHARS = 4000

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
# A field name with one of these segments is also key-named, for example ``order_seed``.
KEY_FIELD_SEGMENTS = frozenset({"seed", "secret", "mnemonic", "privkey", "passphrase"})
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
# Field name segments that name a hash or identifier.
_IDENTIFIER_SEGMENTS = frozenset({"hash", "digest", "txid", "txhash", "id", "ids", "tx", "txn", "checksum", "sha"})
# Normalized field names whose 0x value in result text is a public hash (exact match only).
_ATTRIBUTED_HASH_FIELDS = _HASH_WORDS | frozenset({
    "approvaltx", "approvaltxhash", "swaptx", "swaptxhash", "lasttx", "lasttxhash",
    "sourceconfighash", "codehash", "requesthash", "contractdigest",
})
# Normalized words that, among the words before a value, cancel a hash label.
# ``key`` alone is excluded because exchange asset keys (``"key": "XXBT"``) are common.
_KEY_CONTEXT_WORDS = frozenset({"private", "privatekey", "privkey", "secret", "secretkey", "seed",
                                "seedphrase", "mnemonic", "recoveryphrase", "signingkey", "masterkey"})
_HASH_CONTEXT_WORDS = 3
_KEY_CONTEXT_SPAN = 5


def normalize_field(name: str) -> str:
    return _NORMALIZE_RE.sub("", name.lower())


def _segments(name: str) -> list[str]:
    return [part.lower() for part in _SEGMENT_RE.findall(name)]


@lru_cache(maxsize=4096)
def is_key_field(name: str | None, *, arguments: bool = False) -> bool:
    """True when a field name, after normalization or by one of its segments, names key material."""
    if name is None:
        return False
    norm = normalize_field(name)
    if norm in KEY_FIELD_NAMES or (arguments and norm in _ARGUMENT_KEY_FIELDS):
        return True
    if any(marker in norm for marker in KEY_FIELD_MARKERS):
        return True
    return any(segment in KEY_FIELD_SEGMENTS for segment in _segments(name))


@lru_cache(maxsize=4096)
def _is_identifier_field(name: str) -> bool:
    """A field named for a hash or identifier, with no key-named part."""
    if is_key_field(name, arguments=True):
        return False
    if normalize_field(name) in _HASH_WORDS:
        return True
    return any(segment in _IDENTIFIER_SEGMENTS for segment in _segments(name))


def _is_int_key(value: int) -> bool:
    return _MIN_INT_KEY <= value < _SECP256K1_N


def _decimal_keys(text: str):
    for match in _DECIMAL_KEY_RE.finditer(text):
        if _is_int_key(int(match.group(0))):
            yield match


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


def _checksum_matches(value: int, count: int) -> bool:
    """``value`` packs ``count`` 11-bit word indexes; True when its checksum bits are correct."""
    checksum_bits = count // 3
    entropy = (value >> checksum_bits).to_bytes((count * 11 - checksum_bits) // 8, "big")
    expected = hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits)
    return expected == value & ((1 << checksum_bits) - 1)


def bip39_checksum_valid(words: list[str] | tuple[str, ...]) -> bool:
    """True when ``words`` is an English BIP-39 phrase of 12-24 words with a valid checksum."""
    count = len(words)
    if count not in _MNEMONIC_LENGTH_SET:
        return False
    index = _bip39_index()
    value = 0
    for word in words:
        position = index.get(word)
        if position is None:
            return False
        value = (value << 11) | position
    return _checksum_matches(value, count)


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


def _text_variants(text: str) -> list[str]:
    """``text`` as scanned for phrases: ASCII text as is; other text NFKC-normalized
    (fullwidth letters become ASCII), once with invisible characters removed and
    once with them read as spaces."""
    if text.isascii():
        return [text]
    normalized = unicodedata.normalize("NFKC", text)
    if not _INVISIBLE_RE.search(normalized):
        return [normalized]
    return [_INVISIBLE_RE.sub("", normalized), _INVISIBLE_RE.sub(" ", normalized)]


def has_mnemonic(text: str) -> bool:
    """A run of at least 12 BIP-39 words; no checksum (the hook's rule)."""
    return any(len(run) >= _MNEMONIC_RUN for variant in _text_variants(text) for run in _word_runs(variant))


def _phrase_runs(text: str, *, case_split: bool):
    """Runs of BIP-39 words as (words, joined) pairs.

    Without ``case_split``, words are the letter runs of the lowercased text,
    whatever their case (``aBaNdOn`` is ``abandon``). With it, a letter run is
    also split where a lowercase letter meets an uppercase one (``wordWord``).
    ``joined[i]`` is True when word ``i`` meets the word before it without a
    plain separator: across an underscore (``word_word``) or at a case change.
    Any other non-letter text separates words without ending the run; a
    non-wordlist word ends it.
    """
    words = bip39_words()
    run: list[str] = []
    joined: list[bool] = []
    end = 0
    if not case_split:
        text = text.lower()
    for match in (_CASED_WORD_RE if case_split else _WORD_RE).finditer(text):
        token = match.group(0).lower()
        gap_joined = match.start() == end or "_" in text[end:match.start()]
        end = match.end()
        if token in words:
            joined.append(bool(run) and gap_joined)
            run.append(token)
            continue
        if len(run) >= _MNEMONIC_RUN:
            yield run, joined
        run, joined = [], []
    if len(run) >= _MNEMONIC_RUN:
        yield run, joined


_LETTER_RUN_RE = re.compile(r"[A-Za-z]{36,200}")


def _segment(letters: str) -> list[str] | None:
    """Split a lowercase letter run into BIP-39 words (3 to 8 letters each), or None."""
    words = bip39_words()
    size = len(letters)
    following: list[int | None] = [None] * (size + 1)
    following[size] = size
    for start in range(size - 3, -1, -1):
        for end in range(start + 3, min(start + 8, size) + 1):
            if following[end] is not None and letters[start:end] in words:
                following[start] = end
                break
    if following[0] is None:
        return None
    parts, position = [], 0
    while position < size:
        end = following[position]
        parts.append(letters[position:end])
        position = end
    return parts


def has_recovery_phrase(text: str, budget: list[int] | None = None) -> bool:
    """A window of BIP-39 words that reads as a recovery phrase (rule 2 in the module docstring).

    The text is read twice: case-insensitively, and split at case changes as
    well. Every window of 12, 15, 18, 21 or 24 words is checked, except an excerpt
    of the wordlist itself (consecutive wordlist entries). A window counts when
    its checksum is valid, or when it is not in alphabetical order and repeats
    no word (12 or 15 words) or at most one (18 to 24 words). A window that
    crosses an underscore or case change counts only with a valid checksum and
    at most one repeated word in four. A letter run of 36 to 200 letters with no
    separator is split into wordlist words and checked the same way. ``budget`` holds the number of checksums
    the caller may still evaluate and is decremented; when it runs out the
    text is reported as a phrase.
    """
    remaining = budget if budget is not None else [_WINDOW_BUDGET]
    index = _bip39_index()
    longest = _MNEMONIC_LENGTHS[-1]
    readings = ((variant, case_split) for variant in _text_variants(text) for case_split in (False, True))
    for variant, case_split in readings:
        for run, joined in _phrase_runs(variant, case_split=case_split):
            positions = [index[word] for word in run]
            for start in range(len(positions) - _MNEMONIC_RUN + 1):
                value, previous, repeats = 0, -1, 0
                ascending = consecutive = True
                crossing = False
                seen: set[int] = set()
                for count, position in enumerate(positions[start:start + longest], 1):
                    value = (value << 11) | position
                    if position in seen:
                        repeats += 1
                    seen.add(position)
                    ascending = ascending and position > previous
                    consecutive = consecutive and (previous < 0 or position == previous + 1)
                    crossing = crossing or (count > 1 and joined[start + count - 1])
                    previous = position
                    if count not in _MNEMONIC_LENGTH_SET or consecutive:
                        continue
                    limit = _PLAIN_REPEATS_SHORT if count <= 15 else _PLAIN_REPEATS_LONG
                    if not crossing and not ascending and repeats <= limit:
                        return True
                    if crossing and repeats > count // 4:
                        continue
                    remaining[0] -= 1
                    if remaining[0] < 0 or _checksum_matches(value, count):
                        return True
        for match in () if case_split else _LETTER_RUN_RE.finditer(variant):
            parts = _segment(match.group(0).lower())
            if parts is not None and len(parts) >= _MNEMONIC_RUN and has_recovery_phrase("_".join(parts), remaining):
                return True
    return False


def _string_leaves(value: Any):
    """String values in a structure, in document order (object keys excluded)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, (list, tuple)):
            stack.extend(reversed(item))
        elif isinstance(item, str):
            yield item


def phrase_across_values(value: Any, budget: list[int] | None = None) -> bool:
    """A recovery phrase whose words are spread over several string values.

    The values are joined in document order with an underscore between them,
    so a window that spans two values counts only with a valid checksum
    (``has_recovery_phrase``). This finds a phrase passed as a list of words
    or split across fields.
    """
    leaves = list(_string_leaves(value))
    return len(leaves) > 1 and has_recovery_phrase(" _ ".join(leaves), budget)


def find_in_text(text: str) -> str | None:
    """Strict scan of a value that should hold no key material: any secret shape."""
    if _XRPL_SEED_RE.search(text):
        return XRPL_SEED
    if _HEX_KEY_RE.search(text) or next(_decimal_keys(text), None) is not None:
        return EVM_KEY
    if has_mnemonic(text):
        return MNEMONIC
    return None


def _key_value_hit(value: Any) -> str | None:
    """A key-named field holding any secret shape."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return EVM_KEY if _is_int_key(value) else None
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
    """One of the three words before ``start`` labels the value as a hash, and no nearby word names a key.

    Only the ``_LOOKBACK_CHARS`` characters before ``start`` are read, so the
    check costs the same for every match.
    """
    window_start = max(0, start - _LOOKBACK_CHARS)
    words = _NAME_RE.findall(text, window_start, start)
    if window_start and words and _NAME_RE.match(text, window_start - 1) and _NAME_RE.match(text, window_start):
        words = words[1:]  # the first word may be cut off by the window
    for word in words[-_KEY_CONTEXT_SPAN:]:
        if normalize_field(word) in _KEY_CONTEXT_WORDS or (
                normalize_field(word) not in KEY_FIELD_NAMES and is_key_field(word)):
            return False
    return any(normalize_field(word) in _HASH_WORDS for word in words[-_HASH_CONTEXT_WORDS:])


def _identifier_attributed(text: str, start: int) -> bool:
    """The value at ``start`` follows a quoted field named exactly as a public hash (``"approval_tx": "0x..."``)."""
    match = _ATTRIBUTED_RE.search(text, max(0, start - _ATTRIBUTION_CHARS), start)
    return bool(match) and normalize_field(match.group(1)) in _ATTRIBUTED_HASH_FIELDS


def _free_text_key_hit(text: str, field: str | None) -> str | None:
    """A 64-hex or integer key in a non-key argument string that is not an identifier."""
    if field is not None and _is_identifier_field(field):
        return None
    prose = field is not None and normalize_field(field) in PROSE_FIELD_NAMES
    whole = text.strip()
    for match in _HEX_KEY_RE.finditer(text):
        if not prose and whole == match.group(0):
            continue
        if _hash_labelled(text, match.start()):
            continue
        return EVM_KEY
    for match in _decimal_keys(text):
        if not _hash_labelled(text, match.start()):
            return EVM_KEY
    return None


def _phrase_at(text: str, position: int) -> bool:
    """At least 12 consecutive wordlist words, the first starting at ``position``."""
    if position >= len(text) or not ("a" <= text[position].lower() <= "z"):
        return False
    words = bip39_words()
    count = 0
    for match in _LETTERS_RE.finditer(text, position):
        if match.group(0).lower() not in words:
            return False
        count += 1
        if count == _MNEMONIC_RUN:
            return True
    return False


def _raw_value_hit(text: str, position: int) -> str | None:
    """The raw value at ``position``, past whitespace, quotes and ``{`` or ``[``, is a secret shape."""
    prefix = _VALUE_PREFIX_RE.match(text, position)
    start = prefix.end() if prefix else position
    if _HEX_KEY_RE.match(text, start):
        return EVM_KEY
    if _XRPL_SEED_RE.match(text, start):
        return XRPL_SEED
    digits = _DECIMAL_KEY_RE.match(text, start)
    if digits and _is_int_key(int(digits.group(0))):
        return EVM_KEY
    if _phrase_at(text, start):
        return MNEMONIC
    return None


def _contextual_text_hit(text: str, *, arguments: bool, parsed: bool = False) -> str | None:
    """Text that names a key field (quoted with ``"`` or ``'``).

    Refused when the raw value after the field name, past whitespace, quotes
    and opening ``{`` or ``[``, is a secret shape. Unless ``parsed`` is True,
    it is also refused when a ``0x`` key appears anywhere in it that is
    neither labelled as a hash nor the value of a field named exactly as a
    public hash (``_ATTRIBUTED_HASH_FIELDS``), when it holds an XRPL seed, or
    when it holds a recovery phrase (``has_recovery_phrase``). Argument scans pass
    ``parsed=True`` for JSON text because ``_json_text_hit`` applies its own
    raw-text rules; result scans always apply the text-wide rules.
    """
    named = False
    for match in _QUOTED_FIELD_RE.finditer(text):
        if not is_key_field(match.group(1), arguments=arguments):
            continue
        named = True
        hit = _raw_value_hit(text, match.end())
        if hit:
            return hit
    if not named or parsed:
        return None
    for match in _PREFIXED_KEY_RE.finditer(text):
        if not _hash_labelled(text, match.start()) and not _identifier_attributed(text, match.start()):
            return EVM_KEY
    if _XRPL_SEED_RE.search(text):
        return XRPL_SEED
    if has_recovery_phrase(text):
        return MNEMONIC
    return None


def _walked_strings(value: Any):
    """Every string and integer value and every object key in a parsed JSON value, as text."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                yield str(key)
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            yield item
        elif isinstance(item, int) and not isinstance(item, bool):
            yield str(item)


def _json_text_hit(text: str, parsed: Any) -> str | None:
    """Raw-text rules for an argument string that parsed as JSON (rule 4 in the module docstring)."""
    hit = _contextual_text_hit(text, arguments=True, parsed=True)
    if hit:
        return hit
    seen: set[str] = set()
    for item in _walked_strings(parsed):
        seen.update(match.group(0) for match in _HEX_KEY_RE.finditer(item))
        seen.update(match.group(0) for match in _DECIMAL_KEY_RE.finditer(item))
    for match in _HEX_KEY_RE.finditer(text):
        if match.group(0) not in seen and not _hash_labelled(text, match.start()):
            return EVM_KEY
    for match in _decimal_keys(text):
        if match.group(0) not in seen and not _hash_labelled(text, match.start()):
            return EVM_KEY
    return None


def find_in_arguments(arguments: Any) -> str | None:
    """Return the secret kind found in MCP tool arguments, or None.

    The rules are in the module docstring. Transaction hashes in hash-named
    fields, as whole field values, or labelled as hashes in text pass.
    """
    budget = [_WINDOW_BUDGET]

    def object_key(key: str) -> str | None:
        if _XRPL_SEED_RE.search(key):
            return XRPL_SEED
        if has_recovery_phrase(key, budget):
            return MNEMONIC
        return _free_text_key_hit(key, "text")

    def walk(value: Any, field: str | None) -> str | None:
        if is_key_field(field, arguments=True):
            hit = _key_value_hit(value)
            if hit:
                return hit
        if isinstance(value, dict):
            for key, item in value.items():
                hit = object_key(str(key)) or walk(item, str(key))
                if hit:
                    return hit
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                hit = walk(item, field)
                if hit:
                    return hit
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            if _is_int_key(value) and not (field is not None and _is_identifier_field(field)):
                return EVM_KEY
            return None
        if isinstance(value, str):
            if _XRPL_SEED_RE.search(value):
                return XRPL_SEED
            if has_recovery_phrase(value, budget):
                return MNEMONIC
            nested = _parse_json_text(value)
            if nested is not None:
                return walk(nested, None) or _json_text_hit(value, nested)
            return _free_text_key_hit(value, field) or _contextual_text_hit(value, arguments=True)
        return None

    return walk(arguments, None) or (MNEMONIC if phrase_across_values(arguments, budget) else None)


def find_in_result(result: Any) -> str | None:
    """Return the secret kind found in a tool result, or None.

    Only key-named fields count, because results routinely carry transaction
    hashes. JSON carried as text content is parsed and walked, and the raw
    text is then checked with ``_contextual_text_hit`` as well, so a parser
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


def _argument_texts(value: Any):
    """String leaves, object keys and integers in tool arguments, as text."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                yield str(key)
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            yield item
        elif isinstance(item, int) and not isinstance(item, bool):
            yield str(item)


def redact_error(message: str, arguments: Any) -> tuple[str, str | None]:
    """Remove argument values that an error message repeats, then scan the rest.

    Every argument string, key or integer of 8 or more characters that appears
    in ``message`` is replaced with ``[argument value]``, and a message longer
    than ``_ERROR_CHARS`` is cut to that length. Returns the redacted message
    and the secret kind found in it (as free text, then as result text), or
    None when it holds none.
    """
    if len(message) > _ERROR_CHARS:
        message = message[:_ERROR_CHARS] + "..."
    values = sorted({text for text in _argument_texts(arguments) if len(text) >= 8}, key=len, reverse=True)
    for text in values:
        if text in message:
            message = message.replace(text, "[argument value]")
    return message, find_in_arguments({"message": message}) or find_in_result(message)


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
