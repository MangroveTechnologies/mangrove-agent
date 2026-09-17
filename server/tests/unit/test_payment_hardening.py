"""Adversarial regressions for accounting, receipt trust and secret handling."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import logging
import multiprocessing
import os
import time
import traceback
from pathlib import Path

import httpx
import pytest

from src.config import app_config
from src.services import spend_service, wallet_manager, x402_payer
from src.shared.db import sqlite
from src.shared.errors import SigningError, X402SpendCapExceeded
from src.shared.private_files import append_private
from src.shared.urls import strip_query

PAYER = "0x" + "11" * 20
NETWORK = "eip155:84532"
TX = "0x" + "ab" * 32
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", .1)
    sqlite.reset_connection()
    sqlite.init_db()
    yield tmp_path / "agent.db"
    sqlite.reset_connection()


def _process_reserve(path, start, result):
    # The window deliberately overlaps reads in the old nontransactional code.
    app_config.DB_PATH = path
    app_config.X402_SPEND_CAP_USD = .1
    sqlite.reset_connection()
    original = spend_service._spent_micro_usd

    def slow_read(*args, **kwargs):
        value = original(*args, **kwargs)
        time.sleep(.2)
        return value

    spend_service._spent_micro_usd = slow_read
    try:
        start.wait(timeout=15)
        spend_service.reserve(value=60000, wallet_address=PAYER, network=NETWORK)
        result.put("reserved")
    except X402SpendCapExceeded:
        result.put("refused")
    finally:
        sqlite.reset_connection()


def _process_reset(path, start, result):
    app_config.DB_PATH = path
    sqlite.reset_connection()
    try:
        start.wait(timeout=15)
        spend_service.reset(.1)
        result.put("reset")
    finally:
        sqlite.reset_connection()


def _run_workers(database, target):
    ctx = multiprocessing.get_context("spawn")
    start, result = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=target, args=(str(database), start, result)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        outcomes = [result.get(timeout=25) for _ in workers]
        for worker in workers:
            worker.join(5)
            assert worker.exitcode == 0
        return outcomes
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        result.close()


def test_budget_is_atomic_across_processes(database):
    assert sorted(_run_workers(database, _process_reserve)) == ["refused", "reserved"]
    assert spend_service.get_status()["spent_usd"] == .06


def test_concurrent_resets_advance_distinct_periods(database):
    before = spend_service._get_state()["period_id"]
    assert _run_workers(database, _process_reset) == ["reset", "reset"]
    assert spend_service._get_state()["period_id"] == before + 2


def test_budget_transaction_rolls_back_on_failure(database, monkeypatch):
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", .06)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic database failure")
    monkeypatch.setattr(spend_service, "_mark_exhausted", fail)
    with pytest.raises(RuntimeError):
        spend_service.reserve(value=60000, wallet_address=PAYER, network=NETWORK)
    assert spend_service.list_payments() == []
    assert spend_service.get_status()["spent_usd"] == 0


def _receipt(**updates):
    return {"success": True, "transaction": TX, "network": NETWORK, "payer": PAYER, **updates}


@pytest.mark.parametrize("bad", [
    {}, {"success": False}, _receipt(success=False), _receipt(transaction="0xabc"),
    _receipt(network="eip155:8453"), _receipt(payer="0x" + "22" * 20),
    _receipt(transaction=None), _receipt(success=1), _receipt(payer=[]),
])
def test_untrusted_receipts_never_claim_settlement(database, bad):
    rid = spend_service.reserve(value=1000, wallet_address=PAYER, network=NETWORK)
    encoded = base64.b64encode(json.dumps(bad).encode()).decode()
    response = httpx.Response(200, headers={"payment-response": encoded})
    assert x402_payer.decode_settlement(response, payer=PAYER, network=NETWORK) is None
    # Direct reconciliation must enforce the same boundary too.
    spend_service.reconcile(rid, status_code=200, settlement=bad)
    assert spend_service.list_payments()[0]["state"] == "authorized"
    assert spend_service.get_status()["spent_usd"] == .001


@pytest.mark.parametrize("status", [402, 400, 429, 500, 502, 503])
def test_repeated_errors_cannot_recycle_budget(database, status):
    rid = spend_service.reserve(value=60000, wallet_address=PAYER, network=NETWORK)
    spend_service.reconcile(rid, status_code=status)
    with pytest.raises(X402SpendCapExceeded):
        spend_service.reserve(value=60000, wallet_address=PAYER, network=NETWORK)
    assert spend_service.get_status()["spent_usd"] == .06


def test_invalid_import_hides_input_and_exception_chain():
    from src.services.secret_vault import vault
    marker = "syntheticauditword"
    token = vault.stash(marker + " abandon" * 11)
    with pytest.raises(SigningError) as caught:
        wallet_manager.import_wallet(vault_token=token)
    assert marker not in str(caught.value)
    assert marker not in "".join(traceback.format_exception(caught.value))


def test_http_and_structured_logs_hide_credentials_and_addresses(capsys):
    import structlog

    from src.shared.logging import configure, get_logger
    early_logger = get_logger("imported_before_configuration")
    previous = structlog.get_config()
    old_handlers = logging.getLogger().handlers[:]
    old_http_level = logging.getLogger("httpx").level
    output = io.StringIO()
    try:
        configure("test")
        logging.getLogger().addHandler(logging.StreamHandler(output))
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
            client.get("https://example.invalid/r?token=SYNTHETIC_QUERY")
        early_logger.info("test", wallet_address=PAYER, private_key="SYNTHETIC_KEY",
                          url="https://user:SYNTHETIC_PASSWORD@example.invalid/r?token=SYNTHETIC_QUERY")
        text = output.getvalue() + capsys.readouterr().err
        assert "SYNTHETIC" not in text
        assert PAYER not in text
        assert "https://example.invalid/r" in text
    finally:
        structlog.configure(**previous)
        logging.getLogger().handlers = old_handlers
        logging.getLogger("httpx").setLevel(old_http_level)


def test_url_sanitizer_removes_userinfo_query_and_fragment():
    assert strip_query("https://user:password@example.invalid/a?key=value#fragment") == "https://example.invalid/a"


def test_private_evidence_creation_and_existing_mode(tmp_path):
    path = tmp_path / "evidence.jsonl"
    append_private(path, "first")
    path.chmod(0o644)
    append_private(path, "second")
    assert path.read_text() == "first\nsecond\n"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        with pytest.raises(OSError):
            append_private(alias, "third")


def test_database_is_private(database):
    if os.name == "posix":
        assert database.stat().st_mode & 0o777 == 0o600


def _script(path):
    spec = importlib.util.spec_from_file_location("audit_script", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url", [
    "https://example.com", "http://127.0.0.1.evil.test", "http://127.0.0.1:9082/path",
    "http://user:pass@localhost", "http://localhost?token=x", "http://localhost#fragment",
    "file:///tmp/a", "http://localhost:invalid", "http://169.254.169.254",
])
def test_secret_entry_refuses_unsafe_destinations(url):
    module = _script("scripts/stash-secret.py")
    with pytest.raises(ValueError):
        module.loopback_url(url)


def test_secret_entry_bypasses_proxies_and_refuses_redirects(monkeypatch):
    module = _script("scripts/stash-secret.py")
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid:1234")
    monkeypatch.setenv("HTTPS_PROXY", "http://example.invalid:1234")
    monkeypatch.setattr(module.urllib.request, "getproxies", lambda: pytest.fail("ambient proxies consulted"))
    captured = []
    def open_request(self, request, timeout):
        captured.append(request)
        raise module.urllib.error.HTTPError(request.full_url, 307, "synthetic", {}, io.BytesIO(b"SYNTHETIC_SECRET"))
    monkeypatch.setattr(module.urllib.request.OpenerDirector, "open", open_request)
    with pytest.raises(ValueError) as caught:
        module.stash("http://localhost:9082", "synthetic-key", "SYNTHETIC_SECRET")
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert len(captured) == 1
    assert captured[0].full_url.startswith("http://127.0.0.1:9082/")
    assert "SECRET" not in os.environ
    assert module.NoRedirect().redirect_request(None, None, 307, None, {}, "https://example.invalid") is None


def test_quote_watcher_omits_arbitrary_remote_content(monkeypatch):
    module = _script("server/scripts/watch_x402_quote.py")
    class Response:
        code = 402
        headers = {"PAYMENT-REQUIRED": base64.b64encode(json.dumps({
            "resource": "SYNTHETIC_PRIVATE_DATA", "accepts": [{
                "scheme": "exact", "network": NETWORK, "payTo": PAYER, "amount": "1000",
                "asset": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                "extra": {"secret": "SYNTHETIC_PRIVATE_DATA"},
            }]}).encode()).decode()}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Opener:
        def open(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    result = module.inspect("https://example.invalid/r?token=SYNTHETIC_QUERY")
    assert result["offers"][0]["amount_usdc"] == "0.001"
    assert "SYNTHETIC" not in json.dumps(result)
    assert PAYER not in json.dumps(result)
    assert "payment_required_header" not in result
    assert "decoded_payment_required" not in result


def test_upgrade_restores_unsafe_legacy_releases(database):
    conn = sqlite.get_connection()
    reasons = ["rejected_by_receiver", "resource_error_not_settled",
               "superseded_by_later_attempt", "signature_refused"]
    ids = []
    for reason in reasons:
        rid = spend_service.reserve(value=1000, wallet_address=PAYER, network=NETWORK)
        spend_service.release(rid, reason=reason)
        ids.append(rid)
    invalid = spend_service.reserve(value=1000, wallet_address=PAYER, network=NETWORK)
    conn.execute("UPDATE x402_payments SET state='settled', transaction_hash=NULL WHERE id=?", (invalid,))
    conn.execute("DELETE FROM _migrations WHERE filename='009_x402_uncertain_authorizations.sql'")
    conn.commit()
    assert sqlite.init_db() == ["009_x402_uncertain_authorizations.sql"]
    states = {row["id"]: row["state"] for row in spend_service.list_payments()}
    assert [states[rid] for rid in ids] == ["authorized", "authorized", "authorized", "released"]
    assert states[invalid] == "authorized"
    assert spend_service.get_status()["spent_usd"] == .004
    assert sqlite.init_db() == []


@pytest.mark.parametrize("payload", [
    {"success": False, "errorReason": "insufficient_funds", "secret": "SYNTHETIC_SECRET"},
    {"success": False, "errorReason": "SYNTHETIC_SECRET", "errorMessage": "SYNTHETIC_SECRET"},
])
def test_failure_receipt_diagnostics_allow_only_safe_metadata(payload):
    from src.shared.x402.diagnostics import payment_response_metadata
    response = httpx.Response(402, headers={
        "payment-response": base64.b64encode(json.dumps(payload).encode()).decode(),
    })
    result = payment_response_metadata(response)
    assert result["settlement_success"] is False
    assert result["requirements_header_present"] is False
    assert "SYNTHETIC_SECRET" not in json.dumps(result)


def test_missing_payer_receipt_is_still_rejected_but_transaction_is_diagnosable():
    from src.shared.x402.diagnostics import payment_response_metadata
    payload = _receipt()
    payload.pop("payer")
    response = httpx.Response(200, headers={
        "x-payment-response": base64.b64encode(json.dumps(payload).encode()).decode(),
    })
    assert x402_payer.decode_settlement(response, payer=PAYER, network=NETWORK) is None
    assert payment_response_metadata(response)["transaction"] == TX
    assert payment_response_metadata(response)["payer_present"] is False



def test_unsigned_refusal_does_not_lock_an_unspent_budget(database, monkeypatch):
    from src.shared.errors import SigningError
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", .001)
    monkeypatch.setattr(wallet_manager, "require_backup_confirmed", lambda *a: None)
    signer = x402_payer.CustodialSigner(PAYER)
    with pytest.raises(SigningError):
        signer.sign_typed_data({"chainId": 84532}, {}, "TransferWithAuthorization",
                              {"value": 1000, "to": PAYER})
    assert spend_service.list_payments()[0]["release_reason"] == "signature_refused"
    status = spend_service.get_status()
    assert status["spent_usd"] == 0
    assert status["exhausted"] is False
    spend_service.reserve(value=1, wallet_address=PAYER)


def test_unsigned_rollback_keeps_other_authorizations_counted(database):
    first = spend_service.reserve(value=60000, wallet_address=PAYER)
    second = spend_service.reserve(value=40000, wallet_address=PAYER)
    assert spend_service.get_status()["exhausted"]
    spend_service.release_unsigned(first)
    assert spend_service.get_status()["spent_usd"] == .04
    assert spend_service.get_status()["exhausted"] is False
    spend_service.reserve(value=60000, wallet_address=PAYER)
    # A repeated rollback must not clear the new genuine latch.
    spend_service.release_unsigned(first)
    assert spend_service.get_status()["exhausted"]
    assert next(p for p in spend_service.list_payments() if p["id"] == second)["state"] == "authorized"


def test_old_period_unsigned_rollback_does_not_clear_new_exhaustion(database):
    old = spend_service.reserve(value=100000, wallet_address=PAYER)
    spend_service.reset(.1)
    spend_service.reserve(value=100000, wallet_address=PAYER)
    spend_service.release_unsigned(old)
    assert spend_service.get_status()["exhausted"]
    assert spend_service.get_status()["spent_usd"] == .1


def test_settled_authorization_cannot_be_released_as_unsigned(database):
    rid = spend_service.reserve(value=100000, wallet_address=PAYER)
    spend_service.settle(rid, transaction=TX)
    spend_service.release_unsigned(rid)
    assert spend_service.get_status()["exhausted"]
    assert spend_service.list_payments()[0]["state"] == "settled"



def test_metadata_migration_preserves_legacy_reservations(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript((sqlite.MIGRATIONS_DIR / "008_x402_spend.sql").read_text())
    conn.execute("INSERT INTO x402_payments (id,period_id,state,amount_micro_usd,wallet_address,created_at,updated_at) "
                 "VALUES ('legacy',1,'authorized',50000,?,'old','old')", (PAYER,))
    conn.commit()
    conn.executescript((sqlite.MIGRATIONS_DIR / "010_x402_authorization_metadata.sql").read_text())
    row = conn.execute("SELECT state,amount_micro_usd,authorization_nonce,asset,valid_after FROM x402_payments").fetchone()
    assert row == ("authorized", 50000, None, None, None)
    conn.close()


def _process_release_and_reset(path, start, result, reservation, action):
    app_config.DB_PATH = path
    app_config.X402_SPEND_CAP_USD = .1
    sqlite.reset_connection()
    try:
        start.wait(timeout=15)
        if action == "release":
            spend_service.release_unsigned(reservation)
        else:
            spend_service.reset(.1)
            spend_service.reserve(value=100000, wallet_address=PAYER)
        result.put("ok")
    finally:
        sqlite.reset_connection()


def test_unsigned_rollback_concurrent_with_new_period_does_not_unlock_it(database):
    rid = spend_service.reserve(value=100000, wallet_address=PAYER)
    ctx = multiprocessing.get_context("spawn")
    start, result = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=_process_release_and_reset,
                          args=(str(database), start, result, rid, action))
               for action in ("release", "reset")]
    try:
        for worker in workers:
            worker.start()
        assert [result.get(timeout=25) for _ in workers] == ["ok", "ok"]
        for worker in workers:
            worker.join(5)
            assert worker.exitcode == 0
        status = spend_service.get_status()
        assert status["period_id"] == 2
        assert status["exhausted"] and status["spent_usd"] == .1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        result.close()
