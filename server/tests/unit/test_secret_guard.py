"""Unit tests for src/shared/secret_guard.py.

The guard is the server-side counterpart of the Claude-only
.claude/hooks/block-wallet-secrets.sh hook. All values below are synthetic
shapes; none is a real key, seed or recovery phrase.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

from src.shared import secret_guard  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
HOOKS = REPO / ".claude" / "hooks"

FAKE_KEY = "0x" + "1f" * 32            # EVM private-key shape
FAKE_TX = "0x" + "ab" * 32             # same shape, used as a transaction hash
FAKE_SEED = "s" + "p" * 28             # XRPL family seed shape
_WORDS = sorted(secret_guard.bip39_words())
FAKE_PHRASE = " ".join(_WORDS[:12])    # 12 wordlist words, not a valid checksum


def _real_phrase(words: int = 12) -> str:
    """A fresh checksummed phrase with distinct words, generated for the test; never a real wallet's.

    Distinct words keep the result independent of the repeated-word limits,
    which a random phrase exceeds with probability below 0.2%.
    """
    from eth_account.hdaccount.mnemonic import Mnemonic
    from eth_account.types import Language

    while True:
        phrase = Mnemonic(Language.ENGLISH).generate(words)
        if len(set(phrase.split())) == words:
            return phrase


REAL_PHRASE = _real_phrase()
PROSE = ("buy ETH when the RSI crosses above thirty and sell it when the price "
         "drops more than two percent from the entry, then wait for the next signal")


def test_wordlist_matches_the_claude_hook_list():
    hook_words = {w.strip() for w in (HOOKS / "bip39-english.txt").read_text().splitlines() if w.strip()}
    assert len(hook_words) == 2048
    assert secret_guard.bip39_words() == hook_words


def test_wordlist_reads_the_repository_copy_without_eth_account(monkeypatch):
    def missing(_package):
        raise ModuleNotFoundError("eth_account")

    monkeypatch.setattr(secret_guard.resources, "files", missing)
    assert secret_guard._wordlist_text().split() == list(secret_guard._bip39_list())


def test_wordlist_falls_back_to_eth_account_without_the_repository_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_guard, "_REPOSITORY_WORDLIST", tmp_path / "missing.txt")
    assert secret_guard._wordlist_text().split() == list(secret_guard._bip39_list())


@pytest.mark.parametrize("arguments,kind", [
    ({"vault_token": FAKE_KEY}, secret_guard.EVM_KEY),
    ({"config": {"private_key": FAKE_KEY}}, secret_guard.EVM_KEY),
    ({"items": [{"secret": FAKE_PHRASE}]}, secret_guard.MNEMONIC),
    ({"query": f"my words are {REAL_PHRASE}"}, secret_guard.MNEMONIC),
    ({"note": f"seed {FAKE_SEED} please"}, secret_guard.XRPL_SEED),
    ({"payload": json.dumps({"private_key": FAKE_KEY})}, secret_guard.EVM_KEY),
    ({"payload": f'"mnemonic": "{FAKE_KEY}"'}, secret_guard.EVM_KEY),
])
def test_arguments_with_secrets_are_refused(arguments, kind):
    assert secret_guard.find_in_arguments(arguments) == kind


@pytest.mark.parametrize("arguments", [
    {},
    {"tx_hash": FAKE_TX, "chain_id": 8453},
    {"query": f"what happened in tx {FAKE_TX}?"},
    {"vault_token": "Zx3kP0_qv7Lr9TmN2bYc4w"},
    {"description": PROSE},
    {"wallet_address": "0x" + "a" * 40, "amount": 1.5, "confirm": True},
])
def test_ordinary_arguments_pass(arguments):
    assert secret_guard.find_in_arguments(arguments) is None


def _text_result(body) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))])


@pytest.mark.parametrize("result,kind", [
    (_text_result({"address": "0x" + "a" * 40, "private_key": FAKE_KEY}), secret_guard.EVM_KEY),
    (_text_result({"wallet": {"seed_phrase": FAKE_PHRASE}}), secret_guard.MNEMONIC),
    (CallToolResult(content=[], structuredContent={"secret": FAKE_SEED}), secret_guard.XRPL_SEED),
    ([TextContent(type="text", text=json.dumps({"key": FAKE_KEY}))], secret_guard.EVM_KEY),
    ({"result": {"mnemonic": FAKE_PHRASE}}, secret_guard.MNEMONIC),
    ([TextContent(type="text", text=f'log line "private_key": {FAKE_KEY}')], secret_guard.EVM_KEY),
])
def test_results_with_secret_fields_are_withheld(result, kind):
    assert secret_guard.find_in_result(result) == kind


@pytest.mark.parametrize("result", [
    _text_result({"tx_hash": FAKE_TX, "status": "confirmed"}),
    _text_result({"vault_token": "Zx3kP0_qv7Lr9TmN2bYc4w", "reveal_cmd": "./scripts/reveal-secret.sh X"}),
    _text_result({"summary": PROSE, "secret_type": "private_key"}),
    [TextContent(type="text", text=f"Swap confirmed in {FAKE_TX}.")],
    {"key": "XXBT", "balance": "0.1"},
])
def test_ordinary_results_pass(result):
    assert secret_guard.find_in_result(result) is None


# --- Parity with the Claude hook --------------------------------------------

# Distinct, unordered wordlist words with a bad checksum, built from wordlist positions so this
# file holds no phrase text.
TYPO_PHRASE = " ".join(_WORDS[i] for i in (2047,2,1407,680,1875,1111,213,1993,1230,1124,359,553))

_HOOK_CASES = [
    (FAKE_KEY, True),
    (TYPO_PHRASE, True),
    (FAKE_PHRASE, False),  # an alphabetical excerpt of the wordlist
    (f"seed {FAKE_SEED}", True),
    (PROSE, False),
    ("0x" + "1f" * 31, False),
    ("hello there", False),
]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="hook needs bash and python3")
@pytest.mark.parametrize("text,blocked", _HOOK_CASES)
def test_hook_user_mode_agrees_with_the_free_text_argument_scan(text, blocked):
    hook = subprocess.run(
        ["bash", str(HOOKS / "block-wallet-secrets.sh"), "--mode", "user"],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": text}),
        capture_output=True, text=True, timeout=20,
    )
    assert hook.returncode == (2 if blocked else 0)
    assert (secret_guard.find_in_arguments({"prompt": text}) is not None) is blocked


# --- Bare hex keys (MetaMask export format) ---------------------------------

BARE_KEY = "1f" * 32                   # 64 hex, no 0x: what MetaMask displays
UPPER_PREFIX_KEY = "0X" + "1f" * 32
SHA256_DIGEST = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.mark.parametrize("arguments", [
    {"private_key": BARE_KEY},
    {"vault_token": BARE_KEY},
    {"private_key": UPPER_PREFIX_KEY},
    {"vault_token": f"  {BARE_KEY}\n"},
    {"query": f"import my wallet {BARE_KEY}"},
])
def test_bare_hex_keys_in_arguments_are_refused(arguments):
    assert secret_guard.find_in_arguments(arguments) == secret_guard.EVM_KEY


@pytest.mark.parametrize("result", [
    _text_result({"private_key": BARE_KEY}),
    _text_result({"wallet": {"private_key": UPPER_PREFIX_KEY}}),
    [TextContent(type="text", text=f'debug "private_key": "{BARE_KEY}"')],
])
def test_bare_hex_keys_in_results_are_withheld(result):
    assert secret_guard.find_in_result(result) == secret_guard.EVM_KEY


@pytest.mark.parametrize("arguments", [
    {"tx_hash": FAKE_TX},
    {"tx_hash": "ab" * 32},
    {"definition_hash": SHA256_DIGEST},
    {"config_hash": SHA256_DIGEST, "source_config_hash": "0x" + SHA256_DIGEST},
    {"order_id": "0x" + "cd" * 32},
    {"digest": SHA256_DIGEST},
    {"dataset": SHA256_DIGEST},
    {"query": f"what happened in transaction {FAKE_TX}?"},
    {"query": f"look up tx {SHA256_DIGEST}"},
    {"description": f"strategy definition hash {SHA256_DIGEST}"},
])
def test_hashes_and_identifiers_in_arguments_pass(arguments):
    assert secret_guard.find_in_arguments(arguments) is None


@pytest.mark.parametrize("result", [
    _text_result({"tx_hash": FAKE_TX, "approval_tx_hash": "0x" + "cd" * 32, "status": "confirmed"}),
    _text_result({"transactions": [{"hash": FAKE_TX}, {"hash": "ef" * 32}]}),
    _text_result({"definition_hash": SHA256_DIGEST, "key": "XXBT"}),
    CallToolResult(content=[], structuredContent={"tx_hash": FAKE_TX, "block_hash": "0x" + "12" * 32}),
    [TextContent(type="text", text=f'Swap confirmed. "key": "XXBT", tx {FAKE_TX}, digest {SHA256_DIGEST}')],
])
def test_results_carrying_transaction_hashes_pass(result):
    assert secret_guard.find_in_result(result) is None


# --- Unlisted key field names and free text -------------------------------

@pytest.mark.parametrize("field", [
    "privateKey", "secret_key", "privKey", "PRIVATE_KEY", "private-key", "evm_private_key",
    "walletPrivateKey", "signing_key",
])
def test_key_like_field_names_are_normalized(field):
    assert secret_guard.find_in_arguments({"arguments": {field: FAKE_KEY}}) == secret_guard.EVM_KEY
    assert secret_guard.find_in_arguments({field: BARE_KEY}) == secret_guard.EVM_KEY
    assert secret_guard.find_in_result(_text_result({field: FAKE_KEY})) == secret_guard.EVM_KEY


@pytest.mark.parametrize("field", ["seedPhrase", "mnemonicPhrase", "recovery_phrase", "MNEMONIC"])
def test_phrase_field_names_are_normalized(field):
    # In a key-named field any 12-word run counts, checksum or not.
    assert secret_guard.find_in_arguments({field: FAKE_PHRASE}) == secret_guard.MNEMONIC
    assert secret_guard.find_in_result(_text_result({field: FAKE_PHRASE})) == secret_guard.MNEMONIC


@pytest.mark.parametrize("arguments", [
    {"query": f"import my wallet {FAKE_KEY}"},
    {"query": FAKE_KEY},
    {"description": f"use key {FAKE_KEY} for this"},
    {"arguments": {"query": f"my key is {FAKE_KEY}"}},
    {"messages": [f"here: {FAKE_KEY}"]},
    {"note": f"what happened in {FAKE_TX}?"},
])
def test_hex_key_in_free_text_is_refused(arguments):
    assert secret_guard.find_in_arguments(arguments) == secret_guard.EVM_KEY


# --- Recovery phrases outside key-named fields -----------------------------

# Kept apart in the source: written next to each other, the two read as one
# 26-word run of wordlist words, and two of its 12-word windows pass the checksum.
CROSSOVER_PROSE = "average cross slow average above zero exit when price drop below level"
THRESHOLD_PROSE = "price above average, volume above average, trend above zero, exit when price below average"
STRATEGY_PROSE = [THRESHOLD_PROSE, CROSSOVER_PROSE]


@pytest.mark.parametrize("text", STRATEGY_PROSE)
def test_strategy_prose_made_of_wordlist_words_passes(text):
    assert secret_guard.has_mnemonic(text)  # it is a 12+ run of wordlist words
    assert secret_guard.find_in_arguments({"description": text}) is None


@pytest.mark.parametrize("words", [12, 15, 18, 21, 24])
def test_checksummed_phrase_is_refused_anywhere(words):
    phrase = _real_phrase(words)
    assert secret_guard.find_in_arguments({"description": phrase}) == secret_guard.MNEMONIC
    assert secret_guard.find_in_arguments({"query": f"restore wallet: {phrase}. thanks"}) == secret_guard.MNEMONIC
    assert secret_guard.find_in_arguments({"text": f"price above average {phrase}"}) == secret_guard.MNEMONIC


def test_checksum_agrees_with_eth_account():
    from eth_account.hdaccount.mnemonic import Mnemonic
    from eth_account.types import Language

    reference = Mnemonic(Language.ENGLISH)
    for words in (12, 15, 18, 21, 24):
        for _ in range(20):
            phrase = reference.generate(words).split()
            assert secret_guard.bip39_checksum_valid(phrase)
            mutated = phrase[:-1] + [_WORDS[(_WORDS.index(phrase[-1]) + 1) % 2048]]
            assert secret_guard.bip39_checksum_valid(mutated) == reference.is_mnemonic_valid(" ".join(mutated))
    assert not secret_guard.bip39_checksum_valid(_WORDS[:11])
    assert not secret_guard.bip39_checksum_valid(["notaword"] * 12)


@pytest.mark.parametrize("layout", [
    lambda words: " ".join(words),
    lambda words: "\n".join(words),
    lambda words: "\n".join(f"{i}. {w}" for i, w in enumerate(words, 1)),
    lambda words: f'my phrase is "{" ".join(words)}".',
    lambda words: f"restore ({' '.join(words)}) please",
])
def test_checksummed_phrase_layouts_are_refused(layout):
    words = _real_phrase(12).split()
    assert secret_guard.find_in_arguments({"query": layout(words)}) == secret_guard.MNEMONIC


def test_key_named_field_counts_any_word_run_without_checksum():
    assert secret_guard.find_in_arguments({"mnemonic": ", ".join(FAKE_PHRASE.split())}) == secret_guard.MNEMONIC


# --- Separators and result-text fallback ------------------------------------

@pytest.mark.parametrize("layout", [
    lambda words: ",".join(words),
    lambda words: ", ".join(words),
    lambda words: "-".join(words),
    lambda words: " ".join(f"{i}. {w}" for i, w in enumerate(words, 1)),
    lambda words: "\n".join(f"{i}) {w}," for i, w in enumerate(words, 1)),
    lambda words: " ".join(words[:6]) + ". " + " ".join(words[6:]),
    lambda words: "; ".join(w.upper() for w in words),
])
def test_checksummed_phrase_with_any_separator_is_refused(layout):
    words = _real_phrase(12).split()
    assert secret_guard.find_in_arguments({"query": layout(words)}) == secret_guard.MNEMONIC


@pytest.mark.parametrize("text", [
    '{"private_key": {"value": "' + FAKE_KEY + '"}',            # truncated JSON, nested object
    '"private_key": [ "' + FAKE_KEY + '" ]',
    "\"secret\": '" + FAKE_KEY + "'",
    "'private_key': '" + FAKE_KEY + "'",
    '"privateKey":\n    "' + BARE_KEY + '"',
    '"private_key": "redacted"; later the value ' + FAKE_KEY + ' was logged',
])
def test_result_text_with_key_field_and_nearby_key_is_withheld(text):
    assert secret_guard.find_in_result([TextContent(type="text", text=text)]) == secret_guard.EVM_KEY


@pytest.mark.parametrize("text", [
    # The JSON parser keeps the last duplicate key, so the parsed object hides the first value.
    '{"private_key": "' + FAKE_KEY + '", "private_key": "redacted"}',
    '{"wallet": {"secret": "' + BARE_KEY + '", "secret": null}}',
])
def test_json_text_results_are_also_scanned_as_raw_text(text):
    assert secret_guard.find_in_result(text) == secret_guard.EVM_KEY


# --- JSON argument text is scanned as raw text too --------------------------

@pytest.mark.parametrize("arguments,kind", [
    # The JSON parser keeps the last duplicate key, so the parsed object hides the first value.
    ({"payload": '{"private_key": "' + FAKE_KEY + '", "private_key": "redacted"}'}, secret_guard.EVM_KEY),
    ({"payload": '{"mnemonic": "' + FAKE_PHRASE + '", "mnemonic": null}'}, secret_guard.MNEMONIC),
    # The structural walk reads keys only as field names.
    ({"payload": '{"' + FAKE_KEY + '": 1}'}, secret_guard.EVM_KEY),
    ({"payload": '{"wallets": {"' + BARE_KEY + '": {"chain": "evm"}}}'}, secret_guard.EVM_KEY),
])
def test_json_text_arguments_are_also_scanned_as_raw_text(arguments, kind):
    assert secret_guard.find_in_arguments(arguments) == kind


def test_json_text_arguments_with_identifiers_pass():
    payload = json.dumps({"strategy_id": "ab" * 32, "tx_hash": FAKE_TX, "definition_hash": SHA256_DIGEST})
    assert secret_guard.find_in_arguments({"payload": payload}) is None


# --- Scan time is linear in the input size -----------------------------------

def _never_checksummed_word() -> str:
    return next(word for word in _WORDS
                if not any(secret_guard.bip39_checksum_valid([word] * n) for n in (12, 15, 18, 21, 24)))


def _concatenated_words(count: int) -> str:
    import random

    rng = random.Random(7)
    return " ".join("".join(rng.choice(["able", "about", "zoo"]) for _ in range(37)) for _ in range(count))


@pytest.mark.parametrize("make_text", [
    lambda n: ("tx " + FAKE_TX + " ") * n,                          # many hash-labelled hex values
    lambda n: '"private_key": "x", ' * (n * 4),                      # many key-named fields
    lambda n: " ".join([_never_checksummed_word()] * (n * 9)),     # one long wordlist run, no checksum
    lambda n: ("act add " * 12 + "xyz ") * (n * 2 // 3),            # many short wordlist runs
    lambda n: ("q" * 150 + " ") * (n // 2),                         # long letter runs
    lambda n: _concatenated_words(n // 2),                          # letter runs made of wordlist words
    lambda n: "q" * (n * 70),                                       # one letter run of a megabyte
], ids=["labelled-hex", "key-fields", "word-run", "short-runs", "letter-runs", "concatenated-words",
        "one-letter-run"])
def test_large_adversarial_text_is_scanned_in_linear_time(make_text):
    """About 1 MB scans well under a second, 2 MB under two, and doubling the input at most triples the time."""
    import time

    secret_guard.bip39_words()  # load the wordlist once, outside the timing
    small, large = make_text(15_000), make_text(30_000)
    assert len(small) >= 1_000_000 and len(large) >= 2_000_000
    for scan in (lambda text: secret_guard.find_in_arguments({"query": text}),
                 lambda text: secret_guard.find_in_arguments({"payload": text}),
                 lambda text: secret_guard.find_in_result(text)):
        timings = []
        for text in (small, large):
            started = time.perf_counter()
            scan(text)
            timings.append(time.perf_counter() - started)
        assert timings[0] < 1.0
        assert timings[1] < 2.0
        assert timings[1] < 3 * timings[0] + 0.05


# --- Key-named flags next to public hashes -----------------------------------

@pytest.mark.parametrize("result", [
    _text_result({"has_private_key": False, "last_tx": FAKE_TX, "status": "confirmed"}),
    _text_result({"mnemonic_backed_up": True, "summary": STRATEGY_PROSE[0]}),
    [TextContent(type="text", text="{'private_key_stored': True, 'approval_tx': '" + FAKE_TX + "'}")],
])
def test_key_named_flags_beside_public_values_pass(result):
    assert secret_guard.find_in_result(result) is None


# --- Identifier exemptions never cover secret-bearing names ------------------

@pytest.mark.parametrize("field", ["order_seed", "seed_hex", "wallet_seed_id", "secretHash", "private_key_tx"])
def test_secret_bearing_field_names_are_never_exempted(field):
    assert secret_guard.find_in_arguments({field: BARE_KEY}) == secret_guard.EVM_KEY
    assert secret_guard.find_in_result(_text_result({field: FAKE_KEY})) == secret_guard.EVM_KEY


@pytest.mark.parametrize("arguments", [
    {"query": f"my private key for order {FAKE_KEY}"},
    {"note": f"seed id {BARE_KEY}"},
    {"liquid": f"value {FAKE_KEY}"},
])
def test_hash_labels_beside_secret_words_do_not_exempt(arguments):
    assert secret_guard.find_in_arguments(arguments) == secret_guard.EVM_KEY


@pytest.mark.parametrize("arguments", [
    {"txHash": "note " + FAKE_TX},
    {"approval_tx_hash": FAKE_TX},
    {"orderId": "ref " + FAKE_TX},
])
def test_segment_identifier_names_still_pass(arguments):
    assert secret_guard.find_in_arguments(arguments) is None


# --- Private keys written as integers ----------------------------------------

INT_KEY = int("1f" * 32, 16)              # a valid secp256k1 scalar, as an integer
SECP256K1_N = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)


@pytest.mark.parametrize("arguments", [
    {"value": INT_KEY},
    {"value": str(INT_KEY)},
    {"private_key": INT_KEY},
    {"query": f"my key is {INT_KEY}"},
    {"payload": json.dumps({"private_key": INT_KEY})},
    {"items": [1, 2, INT_KEY]},
])
def test_integer_encoded_keys_in_arguments_are_refused(arguments):
    assert secret_guard.find_in_arguments(arguments) == secret_guard.EVM_KEY


def test_integer_encoded_key_in_result_is_withheld():
    assert secret_guard.find_in_result(_text_result({"private_key": INT_KEY})) == secret_guard.EVM_KEY
    assert secret_guard.find_in_result({"wallet": {"secret": str(INT_KEY)}}) == secret_guard.EVM_KEY


@pytest.mark.parametrize("arguments", [
    {"amount": 10 ** 30},                     # 1e12 tokens with 18 decimals
    {"amount_wei": str(2 ** 128)},
    {"allowance": 2 ** 256 - 1},              # unlimited approval: above the curve order
    {"allowance": str(SECP256K1_N)},          # not a valid scalar
    {"token_id": INT_KEY},                    # uint256 token ids are identifiers
    {"query": f"what is tx {INT_KEY}?"},
    {"flag": True, "count": 12},
])
def test_large_amounts_and_identifiers_pass(arguments):
    assert secret_guard.find_in_arguments(arguments) is None


# --- Parsed JSON results keep the text-wide rules ----------------------------

@pytest.mark.parametrize("text,kind", [
    # A key used as an object key: the structural walk reads keys only as field names.
    ('{"private_key": null, "' + FAKE_KEY + '": "x"}', secret_guard.EVM_KEY),
    # An identifier-looking field that is not an exactly named hash field.
    ('{"wallet_secret_saved": true, "settlement_id": "' + FAKE_KEY + '"}', secret_guard.EVM_KEY),
    ('{"has_private_key": true, "note": "' + FAKE_KEY + '"}', secret_guard.EVM_KEY),
    # A checksummed phrase used as an object key beside a key-named field.
    ('{"mnemonic": null, "' + REAL_PHRASE + '": 1}', secret_guard.MNEMONIC),
    ('{"seed_phrase": "", "backup": {"' + FAKE_SEED + '": true}}', secret_guard.XRPL_SEED),
])
def test_parsed_json_results_still_apply_text_wide_rules(text, kind):
    assert secret_guard.find_in_result([TextContent(type="text", text=text)]) == kind


def test_transaction_hash_under_an_exact_hash_field_passes():
    text = json.dumps({"has_private_key": False, "approval_tx": FAKE_TX, "tx_hash": "0x" + "cd" * 32})
    assert secret_guard.find_in_result([TextContent(type="text", text=text)]) is None


# --- Recovery phrases without a valid checksum --------------------------------

def _fixture_phrase(label: bytes, words: int) -> list[str]:
    """A checksummed phrase derived from a public label; it guards no funds."""
    import hashlib

    entropy = hashlib.sha256(label).digest()[:words * 4 // 3]
    bits = words // 3
    value = (int.from_bytes(entropy, "big") << bits) | (hashlib.sha256(entropy).digest()[0] >> (8 - bits))
    phrase = [_WORDS[(value >> (11 * (words - 1 - i))) & 2047] for i in range(words)]
    assert secret_guard.bip39_checksum_valid(phrase) and len(set(phrase)) == words
    return phrase


def _mistyped(phrase: list[str]) -> list[str]:
    for word in _WORDS:
        typo = phrase[:4] + [word] + phrase[5:]
        if word not in phrase and not secret_guard.bip39_checksum_valid(typo):
            return typo
    raise AssertionError("no typo found")


PHRASES = {12: _fixture_phrase(b"secret-guard fixture 12", 12),
           24: _fixture_phrase(b"secret-guard fixture 24b", 24)}


@pytest.mark.parametrize("words", [12, 24])
@pytest.mark.parametrize("variant", ["valid", "one_wrong_word"])
def test_phrase_with_or_without_valid_checksum_is_caught(words, variant):
    phrase = " ".join(PHRASES[words] if variant == "valid" else _mistyped(PHRASES[words]))
    assert secret_guard.find_in_arguments({"query": f"restore: {phrase}"}) == secret_guard.MNEMONIC
    assert secret_guard.find_in_arguments({"payload": json.dumps({"words": phrase})}) == secret_guard.MNEMONIC
    result_text = json.dumps({"mnemonic": "redacted", "note": phrase})
    assert secret_guard.find_in_result([TextContent(type="text", text=result_text)]) == secret_guard.MNEMONIC
    assert secret_guard.find_in_result(f'"seed_phrase": see below\n{phrase}') == secret_guard.MNEMONIC
    message, found = secret_guard.redact_error(f"restore failed for {phrase}", {})
    assert found == secret_guard.MNEMONIC


@pytest.mark.parametrize("text", [
    " ".join(_WORDS[:24]),                                         # an excerpt of the wordlist
    " ".join(_WORDS[100:130]),
    "input_token input_token input_amount input_amount output_token output_token output_amount output_amount",
    "entry price entry amount entry time exit price exit amount exit time",
    THRESHOLD_PROSE,
    CROSSOVER_PROSE,
])
def test_wordlist_excerpts_and_repetitive_text_pass(text):
    assert not secret_guard.has_recovery_phrase(text)
    assert secret_guard.find_in_arguments({"description": text}) is None


def _tracked_text_files():
    names = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
    for name in filter(None, names.decode().split("\0")):
        try:
            yield name, (REPO / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
def test_no_tracked_repository_file_reads_as_a_recovery_phrase():
    hits = [name for name, text in _tracked_text_files() if secret_guard.has_recovery_phrase(text)]
    assert hits == []


def _with_valid_last_word(words: list[str], above: int = -1) -> list[str]:
    """``words`` with the last word replaced by the first one that makes the checksum valid."""
    for candidate in _WORDS[above + 1:]:
        if secret_guard.bip39_checksum_valid(words[:-1] + [candidate]):
            return words[:-1] + [candidate]
    raise AssertionError("no checksum-valid last word")


def _assert_caught_everywhere(phrase: str) -> None:
    assert secret_guard.find_in_arguments({"query": phrase}) == secret_guard.MNEMONIC
    result_text = json.dumps({"mnemonic": "redacted", "note": phrase})
    assert secret_guard.find_in_result([TextContent(type="text", text=result_text)]) == secret_guard.MNEMONIC
    assert secret_guard.redact_error(f"restore failed: {phrase}", {})[1] == secret_guard.MNEMONIC


@pytest.mark.parametrize("words", [12, 24])
def test_checksum_valid_phrase_with_repeated_words_is_caught(words):
    phrase = list(PHRASES[words])
    phrase[3], phrase[6], phrase[9] = phrase[0], phrase[1], phrase[0]  # three repeats
    phrase = _with_valid_last_word(phrase)
    assert len(set(phrase)) <= words - 3 and secret_guard.bip39_checksum_valid(phrase)
    _assert_caught_everywhere(" ".join(phrase))


@pytest.mark.parametrize("words", [12, 24])
def test_checksum_valid_phrase_in_alphabetical_order_is_caught(words):
    step = 1500 // words
    positions = [i * step + 3 for i in range(words - 1)]          # ascending, with gaps
    phrase = _with_valid_last_word([_WORDS[p] for p in positions] + [_WORDS[0]], above=positions[-1])
    assert phrase == sorted(phrase) and secret_guard.bip39_checksum_valid(phrase)
    _assert_caught_everywhere(" ".join(phrase))


@pytest.mark.parametrize("words", [12, 24])
@pytest.mark.parametrize("around", [("zoo", "zone"), ("first", "first")])
def test_phrase_with_wordlist_words_around_it_is_caught(words, around):
    phrase = PHRASES[words]
    for variant in (phrase, _mistyped(phrase)):
        text = " ".join([around[0], *variant, around[1]])
        _assert_caught_everywhere(text)
        repeated = " ".join([variant[0], *variant, variant[-1]])  # a repeat on each side
        _assert_caught_everywhere(repeated)


# --- Phrase encodings ------------------------------------------------------------

def _random_letters(count: int, seed: int) -> str:
    import random

    return "".join(random.Random(seed).choices("abcdefghijklmnopqrstuvwxyz", k=count))


def _alternate(text: str) -> str:
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(text))


def _fullwidth(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if c.isalpha() else c for c in text)


ENCODINGS = {
    "underscores": lambda p: {"query": "_".join(p)},
    "mixed separators": lambda p: {"query": "".join(w + "_-.\n"[i % 4] for i, w in enumerate(p))},
    "upper case": lambda p: {"query": " ".join(p).upper()},
    "title case": lambda p: {"query": " ".join(p).title()},
    "zero-width between words": lambda p: {"query": "​".join(p)},
    "zero-width inside words": lambda p: {"query": " ".join(w[:2] + "​" + w[2:] for w in p)},
    "unicode spaces": lambda p: {"query": "  　".join(p)},
    "fullwidth letters": lambda p: {"query": _fullwidth(" ".join(p))},
    "punctuation": lambda p: {"query": " / ".join(", ".join(p[i:i + 2]) for i in range(0, len(p), 2))},
    "line wraps": lambda p: {"query": "\r\n".join(" ".join(p[i:i + 5]) for i in range(0, len(p), 5))},
    "numbered list": lambda p: {"query": "\n".join(f"{i}) {w}" for i, w in enumerate(p, 1))},
    "JSON array text": lambda p: {"query": json.dumps(p)},
    "list argument": lambda p: {"words": list(p)},
    "list of objects": lambda p: {"words": [{"n": i, "w": w} for i, w in enumerate(p)]},
    "split across fields": lambda p: {"first": " ".join(p[:5]), "second": " ".join(p[5:])},
    "camel case": lambda p: {"query": p[0] + "".join(w.title() for w in p[1:])},
    "alternating case": lambda p: {"query": _alternate(" ".join(p))},
    "alternating case, underscores": lambda p: {"query": _alternate("_".join(p))},
    "inner capital": lambda p: {"query": " ".join(w[:3] + w[3:].capitalize() for w in p)},
    "all caps, underscores": lambda p: {"query": "_".join(p).upper()},
    "pascal case": lambda p: {"query": "".join(w.title() for w in p)},
    "fullwidth, alternating case": lambda p: {"query": _fullwidth(_alternate(" ".join(p)))},
    "no separator": lambda p: {"query": "".join(p)},
    "no separator, alternating case": lambda p: {"query": _alternate("".join(p))},
    "no separator, digits around": lambda p: {"query": "1" + "".join(p) + "2"},
    "no separator, unicode letters around": lambda p: {"query": "é" + "".join(p) + "١"},
    "no separator, random letters around": lambda p: {"query": "qzxj" * 60 + "".join(p) + "vqkx" * 60},
    "no separator, inside a 5000-letter run": lambda p: {"query": "q" * 2500 + "".join(p) + "x" * 2500},
    "no separator, camel case inner capitals": lambda p: {"query": "".join(w[:2] + w[2:].upper() for w in p)},
    "no separator, random letters inside 5000": lambda p: {"query": _random_letters(2500, 3) + "".join(p)
                                                                    + _random_letters(2500, 4)},
}


@pytest.mark.parametrize("words", [12, 24])
@pytest.mark.parametrize("encoding", sorted(ENCODINGS))
def test_valid_phrase_encodings_are_caught(words, encoding):
    assert secret_guard.find_in_arguments(ENCODINGS[encoding](PHRASES[words])) == secret_guard.MNEMONIC


@pytest.mark.parametrize("arguments", [
    {"fields": ["input_token", "input_amount", "output_token", "output_amount", "slippage_bps"] * 4},
    {"query": "_".join(["input_token", "input_amount", "output_token", "output_amount"] * 4)},
    {"query": "inputTokenInputAmountOutputTokenOutputAmountEntryPriceExitPrice"},
    {"query": "InputTokenOutputAmount" * 40},
    {"side": "buy", "order": "market", "time": "day", "symbol": "ETH", "venue": "base", "note": "open long"},
])
def test_identifiers_and_enum_values_pass(arguments):
    assert secret_guard.find_in_arguments(arguments) is None


def _base64(size: int, seed: int) -> str:
    import base64
    import random

    return base64.b64encode(random.Random(seed).randbytes(size)).decode()


@pytest.mark.parametrize("text", [
    "deadbeefcafe" * 40,                                  # hex letters only
    "0x" + "ab" * 32 + "cdef" * 30,
    "fedcba" * 100,
    _base64(30_000, 1),                                   # long base64
    "".join(_base64(48, seed) for seed in range(400)),
    "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq " * 50,  # bech32 address
    "rPEPPER7kfTD9w2To4CQk6UCfuHM9c6GDY " * 50,         # XRPL classic address
    "0x" + "a" * 40 + " " + "0x" + "b0" * 20,             # EVM addresses
])
def test_hex_base64_and_address_strings_are_not_phrases(text):
    assert not secret_guard.has_recovery_phrase(text)


def test_window_budget_fails_closed_before_the_work():
    """Text needing more window starts than the budget allows is refused as a possible phrase."""
    import time

    run = " ".join([_never_checksummed_word()] * (secret_guard._WINDOW_BUDGET + 100))
    started = time.perf_counter()
    assert secret_guard.has_recovery_phrase(run)
    assert time.perf_counter() - started < 1.0
    assert secret_guard.find_in_arguments({"query": run}) == secret_guard.MNEMONIC
    budget = [0]
    assert secret_guard.has_recovery_phrase(" ".join(PHRASES[12][:6] * 2), budget)  # no budget left


@pytest.mark.parametrize("text,found", [
    ("q" * 2_000_000, None),
    (_random_letters(2_000_000, 5), None),
    ("".join(PHRASES[12]) * 30_000, secret_guard.MNEMONIC),
    ("InputTokenOutputAmount" * 90_000, secret_guard.MNEMONIC),  # past the window budget: fails closed
], ids=["one-letter", "random-letters", "repeated-phrase", "camel-identifiers-past-budget"])
def test_two_megabytes_of_letters_finish_in_under_a_second(text, found):
    import time

    secret_guard.bip39_words()
    started = time.perf_counter()
    assert secret_guard.find_in_arguments({"query": text}) == found
    assert time.perf_counter() - started < 1.0


# --- Error messages are scanned whole, before they are cut or redacted ---------

def test_key_split_by_the_error_length_cut_is_withheld():
    message = "x " * ((secret_guard._ERROR_CHARS - 63) // 2) + "key " + BARE_KEY
    assert secret_guard.redact_error(message, {}) == ("", secret_guard.EVM_KEY)


def test_phrase_split_by_the_error_length_cut_is_withheld():
    phrase = " ".join(PHRASES[12])
    message = "x" * (secret_guard._ERROR_CHARS - len(" ".join(PHRASES[12][:11])) - 1) + " " + phrase
    assert secret_guard.redact_error(message, {}) == ("", secret_guard.MNEMONIC)


def test_phrase_broken_by_redacting_an_argument_word_is_withheld():
    phrase = PHRASES[12]
    label = " ".join(phrase[4:6])  # two words of the phrase, long enough to be redacted
    assert len(label) >= 8
    assert secret_guard.redact_error(f"restore failed: {' '.join(phrase)}", {"label": label})[1] == secret_guard.MNEMONIC


def test_secret_shaped_argument_echo_is_redacted_not_withheld():
    digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    assert secret_guard.redact_error(f"unknown dataset {digest}", {"dataset": digest}) == (
        "unknown dataset [argument value]", None)


def test_huge_error_message_with_many_argument_values_is_bounded():
    import time

    arguments = {"items": [f"value-{i:06d}" for i in range(20_000)]}
    message = " ".join(arguments["items"]) * 20
    started = time.perf_counter()
    redacted, found = secret_guard.redact_error(message, arguments)
    assert time.perf_counter() - started < 1.0
    assert found is None and len(redacted) <= secret_guard._ERROR_CHARS + 3


def test_phrase_padded_across_the_error_cut_is_withheld():
    phrase = PHRASES[12]
    message = "x" * 3000 + " " + " ".join(phrase[:11]) + " " * 5000 + phrase[11]
    assert secret_guard.redact_error(message, {}) == ("", secret_guard.MNEMONIC)


def test_huge_single_argument_echoed_in_an_error_is_bounded():
    import time

    big = "v" * 2_000_000
    started = time.perf_counter()
    redacted, found = secret_guard.redact_error(big + " failed", {"payload": big})
    assert time.perf_counter() - started < 2.0
    assert found is None and len(redacted) <= secret_guard._ERROR_CHARS + 3
