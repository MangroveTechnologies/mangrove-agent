"""Observer reads summaries only and never creates/mutates the payment DB."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("watch_payments", Path(__file__).resolve().parents[2] / "scripts/watch_x402_payments.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


def test_missing_db_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        watch.snapshot(path)
    assert not path.exists()


def test_evidence_omits_secrets_and_nested_data():
    result = watch.evidence_event(json.dumps({"event": "response", "status": 402,
        "payment-signature": "SYNTHETIC_SECRET", "body": "SYNTHETIC_SECRET",
        "transaction": {"secret": "SYNTHETIC_SECRET"}}))
    assert result == {"event": "response", "status": 402}
    assert watch.evidence_event('{"event":"ledger","payments":[]}') is None


def test_tail_follows_complete_new_records_and_rotation(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event":"start"}\n')
    tail = watch.EvidenceTail(path)
    assert tail.read() == []
    with path.open("a") as f:
        f.write('{"event":"PASS"}')
    assert tail.read() == []
    with path.open("a") as f:
        f.write('\n')
    assert tail.read() == [{"event": "PASS"}]
    path.unlink()
    path.write_text('{"event":"FAIL"}\n')
    assert tail.read() == [{"event": "FAIL"}]


def test_row_settlement_is_labelled_server_evidence():
    row = dict(id="r", state="settled", period_id=1, amount_micro_usd=1000,
               wallet_address="0x" + "11" * 20, payee="0x" + "22" * 20,
               network="eip155:84532", resource="https://example.test", transaction_hash="0x" + "ab" * 32)
    event = watch.payment_event(row)
    assert event["amount_usdc"] == "0.001"
    assert event["evidence"] == "server_receipt"
    assert "onchain_verified" not in event



def test_test_mcp_attempt_marker_survives_restarts(tmp_path):
    spec = importlib.util.spec_from_file_location("test_mcp", Path(__file__).resolve().parents[2] / "scripts/claude_x402_test_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "attempt"
    module.claim_attempt(path)
    with pytest.raises(FileExistsError):
        module.claim_attempt(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_event_shape_is_not_forwarded():
    assert watch.evidence_event('{"event":{}}') is None
