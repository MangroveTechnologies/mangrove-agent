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
PROSE = ("buy ETH when the RSI crosses above thirty and sell it when the price "
         "drops more than two percent from the entry, then wait for the next signal")


def test_wordlist_matches_the_claude_hook_list():
    hook_words = {w.strip() for w in (HOOKS / "bip39-english.txt").read_text().splitlines() if w.strip()}
    assert len(hook_words) == 2048
    assert secret_guard.bip39_words() == hook_words


@pytest.mark.parametrize("arguments,kind", [
    ({"vault_token": FAKE_KEY}, secret_guard.EVM_KEY),
    ({"config": {"private_key": FAKE_KEY}}, secret_guard.EVM_KEY),
    ({"items": [{"secret": FAKE_PHRASE}]}, secret_guard.MNEMONIC),
    ({"query": f"my words are {FAKE_PHRASE}"}, secret_guard.MNEMONIC),
    ({"note": f"seed {FAKE_SEED} please"}, secret_guard.XRPL_SEED),
    ({"payload": json.dumps({"private_key": FAKE_KEY})}, secret_guard.EVM_KEY),
    ({"payload": f'"mnemonic": "{FAKE_KEY}"'}, secret_guard.EVM_KEY),
])
def test_arguments_with_secrets_are_refused(arguments, kind):
    assert secret_guard.find_in_arguments(arguments) == kind


@pytest.mark.parametrize("arguments", [
    {},
    {"tx_hash": FAKE_TX, "chain_id": 8453},
    {"query": f"what happened in {FAKE_TX}?"},
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
        input=text, capture_output=True, text=True, timeout=20,
    )
    assert hook.returncode == (2 if blocked else 0)
    assert (secret_guard.find_in_text(text) is not None) is blocked
