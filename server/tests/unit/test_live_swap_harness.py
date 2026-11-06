"""Offline contract checks for the explicitly opt-in funded HTTP harness."""
import json

import httpx
import pytest

from tests.e2e import test_live_swap as live

WALLET = "0x" + "11" * 20
ENVIRONMENT = {"ENABLE_MAINNET_TEST": "1", "LIVE_AGENT_URL": "http://127.0.0.1:9080",
               "LIVE_AGENT_API_KEY": "synthetic-local-key", "LIVE_WALLET_ADDRESS": WALLET}


def metadata(**overrides):
    return {"address": WALLET, "chain": "evm", "chain_id": 8453,
            "custody_status": "isolated", "backup_confirmed_at": "2026-11-05T00:00:00Z", **overrides}


@pytest.mark.parametrize("url", ["https://evil.example:443", "http://localhost:9080", "http://127.0.0.1",
    "http://127.0.0.1:9080/path", "http://user:password@127.0.0.1:9080", "http://127.0.0.1:9080?key=x"])
def test_local_credential_never_goes_to_unapproved_origin(url):
    with pytest.raises(ValueError, match="numeric loopback"):
        live._live_settings({**ENVIRONMENT, "LIVE_AGENT_URL": url})


def test_funded_harness_requires_explicit_authorization_and_no_secret_in_repr():
    with pytest.raises(ValueError, match="explicit"):
        live._live_settings({**ENVIRONMENT, "ENABLE_MAINNET_TEST": "0"})
    settings = live._live_settings(ENVIRONMENT)
    assert "synthetic-local-key" not in repr(settings)


def test_exact_human_amount_and_decimal_slippage_over_http():
    calls = []

    def service(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[metadata()])
        body = json.loads(request.content)
        assert body["amount"] == 0.1
        assert body["slippage_pct"] == 0.002
        assert body["wallet_address"] == WALLET
        assert body["chain_id"] == 8453
        assert body["confirm"] is True
        assert "slippage" not in body
        assert not {"private_key", "secret", "vault_token"}.intersection(body)
        return httpx.Response(200, json={"status": "confirmed", "tx_hash": "0x" + "22" * 32})

    result = live._run_live_swap(live._live_settings(ENVIRONMENT), transport=httpx.MockTransport(service))
    assert result["status"] == "confirmed"
    assert [request.method for request in calls] == ["GET", "POST"]
    assert all(request.headers["X-API-Key"] == "synthetic-local-key" for request in calls)


@pytest.mark.parametrize("changes", [{"custody_status": "migration_required"}, {"backup_confirmed_at": None},
                                    {"chain_id": 84532}, {"address": "0x" + "99" * 20}])
def test_unprepared_wallet_never_receives_a_swap_post(changes):
    calls = []

    def service(request):
        calls.append(request)
        return httpx.Response(200, json=[metadata(**changes)])

    with pytest.raises(AssertionError, match="no swap requested"):
        live._run_live_swap(live._live_settings(ENVIRONMENT), transport=httpx.MockTransport(service))
    assert [request.method for request in calls] == ["GET"]


def test_failed_or_redirected_swap_never_retries_or_prints_response_body():
    calls = []

    def service(request):
        calls.append(request)
        return (httpx.Response(200, json=[metadata()]) if request.method == "GET"
                else httpx.Response(307, headers={"Location": "https://evil.example"}, text="synthetic-sensitive-body"))

    with pytest.raises(AssertionError, match="Do not rerun automatically") as error:
        live._run_live_swap(live._live_settings(ENVIRONMENT), transport=httpx.MockTransport(service))
    assert "synthetic-sensitive-body" not in str(error.value)
    assert [request.method for request in calls] == ["GET", "POST"]
