"""Offline coverage of the preserved command-line payment entry points."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from src.services.x402_payer import PaymentResult

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SPEC = importlib.util.spec_from_file_location("_x402_demo_test", SCRIPTS / "_x402_demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)
ENTRY_POINTS = ["pay_hello_mangrove.py", "agent_pay_hello_mangrove.py",
                "agent_pay_hello_mangrove_mcp.py", "test_x402_mainnet.py"]


@pytest.mark.parametrize("script", ENTRY_POINTS)
def test_help_without_config_dependencies_or_state(script, tmp_path):
    env = dict(os.environ)
    for key in ("ENVIRONMENT", "APP_ENV", "MANGROVE_AGENT_HOME", "PYTHONPATH"):
        env.pop(key, None)
    result = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), "--help"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--wallet" in result.stdout and "--allow-mainnet" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("origin", ["http://127.0.0.1:9082", "https://localhost:9080/", "http://[::1]:9080"])
def test_local_origins(origin):
    assert demo.local_origin(origin) == origin.rstrip("/")


@pytest.mark.parametrize("origin", ["https://outside.test", "http://localhost.evil.test",
    "http://user:secret@localhost", "http://localhost/path", "http://localhost?secret=x",
    "http://localhost#secret", "file:///tmp/a", "not a url"])
def test_unsafe_origins_are_refused_without_echo(origin):
    with pytest.raises(demo.DemoError) as caught:
        demo.local_origin(origin)
    assert origin not in str(caught.value)


@pytest.mark.parametrize("db_path", ["missing.db", ":memory:"])
def test_missing_state_never_creates_database(tmp_path, db_path):
    cfg = SimpleNamespace(DB_PATH=db_path, MASTER_KEY_PATH="master.key")
    with pytest.raises(demo.DemoError, match="Existing agent database"):
        demo.existing_state(cfg, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_existing_database_paths_are_cwd_independent(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    state = tmp_path / "state"
    state.mkdir()
    path = state / "agent.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE _migrations (filename TEXT)")
        for migration in (demo.SERVER_ROOT / "src/shared/db/migrations").glob("*.sql"):
            conn.execute("INSERT INTO _migrations VALUES (?)", (migration.name,))
        conn.commit()
    before = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    cfg = SimpleNamespace(DB_PATH="agent.db", MASTER_KEY_PATH="master.key")
    demo.existing_state(cfg, state)
    assert cfg.DB_PATH == str(path)
    assert cfg.MASTER_KEY_PATH == str(state / "master.key")
    assert before == path.read_bytes()
    # Already anchored plugin/absolute paths are not rebased by --state-dir.
    demo.existing_state(cfg, tmp_path / "different")
    assert cfg.DB_PATH == str(path)


def test_unmigrated_database_is_not_modified(tmp_path):
    path = tmp_path / "agent.db"
    path.touch()
    with pytest.raises(demo.DemoError, match="Cannot read"):
        demo.existing_state(SimpleNamespace(DB_PATH=str(path)), tmp_path)
    assert path.read_bytes() == b""


@pytest.mark.parametrize("network", ["eip155:8453", "", "eip155:1"])
async def test_network_safety_before_state_or_network(monkeypatch, network):
    from src.config import app_config
    from src.services import x402_payer

    monkeypatch.setattr(app_config, "LOCAL_AGENT_URL", "http://127.0.0.1:9080")
    monkeypatch.setattr(x402_payer, "get_network", lambda: network)
    monkeypatch.setattr(demo, "existing_state", lambda *a: pytest.fail("state touched"))
    with pytest.raises(demo.DemoError):
        await demo.run("rest", demo.parser("rest").parse_args([]))


@pytest.mark.parametrize("status,paid,expected", [(200, True, 0), (200, False, 1), (502, True, 1), (402, False, 1)])
def test_success_requires_resource_and_receipt(status, paid, expected, capsys):
    result = PaymentResult(status, {"message": "hello\x1b[31m"}, paid,
                           "0x" + "ab" * 32, "eip155:84532", "0x" + "11" * 20)
    assert demo.report(result) == expected
    output = capsys.readouterr().out
    assert "\x1b" not in output
    if paid:
        assert "https://sepolia.basescan.org/tx/" in output
    else:
        assert "https://" not in output


@pytest.mark.parametrize("error", [RuntimeError("SYNTHETIC_SECRET"), TimeoutError("SYNTHETIC_SECRET")])
def test_cli_errors_do_not_dump_remote_text(monkeypatch, capsys, error):
    async def fail(*args):
        raise error
    monkeypatch.setattr(demo, "run", fail)
    assert demo.main("mcp", []) == 1
    output = capsys.readouterr().err
    assert "SYNTHETIC_SECRET" not in output
    assert "ledger" in output


def test_cli_cancellation(monkeypatch, capsys):
    async def fail(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(demo, "run", fail)
    assert demo.main("walkthrough", []) == 130
    assert "ledger" in capsys.readouterr().err


def _quote(network="eip155:84532"):
    value = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": network,
             "asset": "0x" + "11" * 20, "amount": "75000", "payTo": "0x" + "22" * 20,
             "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}]}
    return base64.b64encode(json.dumps(value).encode()).decode()


@pytest.mark.parametrize("status,header,valid", [(402, _quote(), True), (200, _quote(), False),
    (302, _quote(), False), (402, "", False), (402, "broken", False),
    (402, "a" * 16385, False), (402, _quote("eip155:8453"), False)])
async def test_unsigned_challenge_is_bounded_and_checked(monkeypatch, capsys, status, header, valid):
    original = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        assert "payment-signature" not in request.headers
        return httpx.Response(status, headers={"payment-required": header})
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    if valid:
        await demo.inspect_quote("http://localhost:9080/api/x402/hello-mangrove", "eip155:84532")
        assert '"amount_base_units": "75000"' in capsys.readouterr().out
    else:
        with pytest.raises(demo.DemoError):
            await demo.inspect_quote("http://localhost:9080/api/x402/hello-mangrove", "eip155:84532")
    assert len(calls) == 1


@pytest.mark.parametrize("mode,interactive,quote,mcp", [("rest", False, False, False),
    ("walkthrough", True, True, False), ("smoke", False, True, False), ("mcp", False, False, True)])
async def test_each_script_role_is_preserved(monkeypatch, mode, interactive, quote, mcp):
    from src.config import app_config
    from src.services import x402_payer

    events = []
    monkeypatch.setattr(app_config, "LOCAL_AGENT_URL", "http://127.0.0.1:9082")
    monkeypatch.setattr(x402_payer, "get_network", lambda: "eip155:84532")
    monkeypatch.setattr(demo, "existing_state", lambda *a: events.append("state"))
    monkeypatch.setattr("src.shared.crypto.fernet.require_existing_master_key", lambda: events.append("key"))
    monkeypatch.setattr("builtins.input", lambda *a: events.append("prompt"))
    async def inspect(*args):
        events.append("quote")
    async def pay(url, **kwargs):
        events.append("rest")
        assert url == "http://127.0.0.1:9082/api/x402/hello-mangrove"
        return PaymentResult(200, {}, True)
    async def mcp_pay(origin, wallet):
        events.append("mcp")
        assert origin == "http://127.0.0.1:9082"
        return PaymentResult(200, [], True)
    monkeypatch.setattr(demo, "inspect_quote", inspect)
    monkeypatch.setattr(x402_payer, "pay", pay)
    monkeypatch.setattr(demo, "mcp_payment", mcp_pay)
    assert await demo.run(mode, demo.parser(mode).parse_args([])) == 0
    assert events == ["state", "key"] + (["prompt"] if interactive else []) + (["quote"] if quote else []) + (["prompt"] if interactive else []) + (["mcp"] if mcp else ["rest"])


@pytest.mark.parametrize("mode", ["rest", "mcp", "walkthrough", "smoke"])
async def test_missing_key_blocks_all_script_modes_before_network(monkeypatch, tmp_path, mode):
    from src.config import app_config
    from src.shared.crypto import fernet
    from src.shared.errors import SigningError

    monkeypatch.setattr(app_config, "LOCAL_AGENT_URL", "http://127.0.0.1:9080")
    monkeypatch.setattr(app_config, "X402_NETWORK", "eip155:84532")
    key_path = tmp_path / "master.key"
    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(key_path))
    monkeypatch.setattr(fernet, "_read_keychain", lambda: None)
    monkeypatch.setattr(demo, "existing_state", lambda *a: None)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("network client constructed"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted before key validation"))
    fernet.reset_master_key_cache()
    try:
        with pytest.raises(SigningError, match="encryption key is unavailable"):
            await demo.run(mode, demo.parser(mode).parse_args([]))
        assert not key_path.exists()
    finally:
        fernet.reset_master_key_cache()


def test_standalone_cli_initializes_redaction_in_fresh_process(tmp_path):
    code = '''
import contextlib, importlib.util, io, json
spec = importlib.util.spec_from_file_location("demo", SCRIPT_PATH)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)
from src.config import app_config
from src.shared.crypto import fernet
from src.services import x402_payer
from src.shared.logging import get_logger
app_config.LOCAL_AGENT_URL = "http://127.0.0.1:9080"
app_config.X402_NETWORK = "eip155:84532"
demo.existing_state = lambda *args: None
fernet.require_existing_master_key = lambda: None
async def fake_pay(*args, **kwargs):
    get_logger("audit").info("synthetic_probe", wallet_address="0x" + "12" * 20,
                             private_key="SYNTHETIC_SECRET", api_key="SYNTHETIC_API_KEY")
    return x402_payer.PaymentResult(200, {}, False)
x402_payer.pay = fake_pay
captured = io.StringIO()
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    demo.main("rest", [])
output = captured.getvalue()
assert "synthetic_probe" in output
assert "SYNTHETIC_SECRET" not in output
assert "SYNTHETIC_API_KEY" not in output
assert "0x" + "12" * 20 not in output
assert "[redacted]" in output
print("Redaction verified")
'''.replace("SCRIPT_PATH", repr(str(SCRIPTS / "_x402_demo.py")))
    env = {**os.environ, "ENVIRONMENT": "test"}
    env.pop("MANGROVE_AGENT_HOME", None)
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Redaction verified"
