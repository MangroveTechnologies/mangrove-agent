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
    """A fresh checksummed phrase generated for the test; never a real wallet's."""
    from eth_account.hdaccount.mnemonic import Mnemonic
    from eth_account.types import Language

    return Mnemonic(Language.ENGLISH).generate(words)


REAL_PHRASE = _real_phrase()
PROSE = ("buy ETH when the RSI crosses above thirty and sell it when the price "
         "drops more than two percent from the entry, then wait for the next signal")


def test_wordlist_matches_the_claude_hook_list():
    hook_words = {w.strip() for w in (HOOKS / "bip39-english.txt").read_text().splitlines() if w.strip()}
    assert len(hook_words) == 2048
    assert secret_guard.bip39_words() == hook_words


def test_wordlist_falls_back_to_the_repository_copy_without_eth_account(monkeypatch):
    def missing(_package):
        raise ModuleNotFoundError("eth_account")

    monkeypatch.setattr(secret_guard.resources, "files", missing)
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

_HOOK_CASES = [
    (FAKE_KEY, True),
    (FAKE_PHRASE, True),
    (f"seed {FAKE_SEED}", True),
    (PROSE, False),
    ("0x" + "1f" * 31, False),
    ("hello there", False),
]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("python3") is None,
                    reason="hook needs bash and python3")
@pytest.mark.parametrize("text,blocked", _HOOK_CASES)
def test_strict_text_scan_agrees_with_the_hook_user_mode(text, blocked):
    hook = subprocess.run(
        ["bash", str(HOOKS / "block-wallet-secrets.sh"), "--mode", "user"],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": text}),
        capture_output=True, text=True, timeout=20,
    )
    assert hook.returncode == (2 if blocked else 0)
    assert (secret_guard.find_in_text(text) is not None) is blocked


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


# --- Recovery phrases need a valid checksum outside key-named fields --------

STRATEGY_PROSE = [
    "price above average, volume above average, trend above zero, exit when price below average",
    "average cross slow average above zero exit when price drop below level",
]


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
