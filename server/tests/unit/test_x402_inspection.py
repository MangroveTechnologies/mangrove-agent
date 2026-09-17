"""Read-only chain evidence never equates a used nonce with a paid resource."""
import pytest

from src.services.x402_inspection import TOKENS, inspect_authorization


def row():
    return {"id": "r", "state": "authorized", "authorization_nonce": "0x" + "ab" * 32,
            "asset": TOKENS["eip155:84532"], "wallet_address": "0x" + "11" * 20,
            "network": "eip155:84532", "valid_before": 100}


@pytest.mark.parametrize("used,timestamp,outcome", [(0, 101, "expired_unused_at_finalized_block"),
    (0, 99, "unused_not_expired"), (0, 100, "unused_not_expired"), (1, 101, "used_or_cancelled")])
def test_finalized_nonce_evidence(used, timestamp, outcome):
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_chainId":
            return hex(84532)
        if method == "eth_getBlockByNumber":
            assert params == ["finalized", False]
            return {"number": "0x10", "hash": "0x" + "cd" * 32, "timestamp": hex(timestamp)}
        assert method == "eth_call"
        assert params[1] == {"blockHash": "0x" + "cd" * 32, "requireCanonical": True}
        assert params[0]["data"].endswith("ab" * 32)
        return "0x" + format(used, "064x")
    result = inspect_authorization(row(), rpc)
    assert result["outcome"] == outcome
    assert not result["ledger_changed"] and not result["payment_sent"]
    assert len(calls) == 3


def test_legacy_row_remains_unresolved_without_network_call():
    legacy = row()
    legacy["authorization_nonce"] = None
    result = inspect_authorization(legacy, lambda *a: pytest.fail("RPC not needed"))
    assert result["outcome"] == "legacy_metadata_missing"


def test_wrong_rpc_chain_refuses():
    with pytest.raises(ValueError, match="network"):
        inspect_authorization(row(), lambda *a: "0x1")
