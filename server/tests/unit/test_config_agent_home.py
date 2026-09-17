"""MANGROVE_AGENT_HOME — plugin installs keep config and state outside the code dir.

Claude Code replaces a plugin's code directory on every update, so config,
the wallet DB and the master key must resolve under MANGROVE_AGENT_HOME, never
relative to the process cwd or the package.
"""
import json
from pathlib import Path

import pytest

from src.config import _Config

PKG_CONFIG_DIR = Path(__file__).resolve().parents[2] / "src" / "config"


def _write_home_config(home: Path, **overrides) -> None:
    cfg = json.loads((PKG_CONFIG_DIR / "test-config.json").read_text())
    cfg.update(overrides)
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "test-config.json").write_text(json.dumps(cfg))


@pytest.fixture
def agent_home(tmp_path, monkeypatch):
    home = tmp_path / "agent-home"
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("MANGROVE_AGENT_HOME", str(home))
    return home


def test_relative_state_paths_resolve_under_agent_home(agent_home):
    _write_home_config(
        agent_home,
        DB_PATH="./agent-data/agent.db",
        MASTER_KEY_PATH="./agent-data/master.key",
    )
    cfg = _Config()
    assert cfg.DB_PATH == str(agent_home / "agent-data" / "agent.db")
    assert cfg.MASTER_KEY_PATH == str(agent_home / "agent-data" / "master.key")


def test_absolute_and_in_memory_paths_are_left_alone(agent_home, tmp_path):
    absolute_key = str(tmp_path / "elsewhere" / "master.key")
    _write_home_config(agent_home, DB_PATH=":memory:", MASTER_KEY_PATH=absolute_key)
    cfg = _Config()
    assert cfg.DB_PATH == ":memory:"
    assert cfg.MASTER_KEY_PATH == absolute_key


def test_missing_config_under_agent_home_fails_loudly(agent_home):
    # No config written: must not silently fall back to the package config.
    with pytest.raises(SystemExit):
        _Config()


def test_without_agent_home_the_package_config_is_used(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("MANGROVE_AGENT_HOME", raising=False)
    expected = json.loads((PKG_CONFIG_DIR / "test-config.json").read_text())
    cfg = _Config()
    assert cfg.DB_PATH == expected["DB_PATH"]
    assert cfg.MASTER_KEY_PATH == expected["MASTER_KEY_PATH"]


@pytest.mark.parametrize("keep_api_key", [True, False])
def test_old_config_without_payment_destinations_still_loads(agent_home, keep_api_key):
    _write_home_config(agent_home)
    path = agent_home / "config" / "test-config.json"
    data = json.loads(path.read_text())
    for name in ("X402_MANGROVE_ENVIRONMENT", "X402_MANGROVE_BASE_URL", "X402_MANGROVE_KB_BASE_URL"):
        data.pop(name, None)
    if not keep_api_key:
        del data["MANGROVE_API_KEY"]
    path.write_text(json.dumps(data))
    config = _Config()
    assert config.MANGROVE_API_KEY == (data["MANGROVE_API_KEY"] if keep_api_key else None)
    assert config.X402_MANGROVE_BASE_URL is None


def test_optional_api_key_still_resolves_secret_reference(agent_home, monkeypatch):
    _write_home_config(agent_home, MANGROVE_API_KEY="secret:sdk:key")
    monkeypatch.setattr("src.config.SecretUtils.get_secret", lambda project, secret, prop: "dev_resolved")
    assert _Config().MANGROVE_API_KEY == "dev_resolved"


def test_existing_install_gets_bundled_endpoints_without_config_migration(agent_home):
    _write_home_config(agent_home)
    path = agent_home / "config" / "test-config.json"
    before = path.read_bytes()
    cfg = _Config()
    assert cfg.MANGROVE_ENDPOINTS["default_environment"]["local"] == "prod"
    assert cfg.MANGROVE_ENDPOINTS["environments"]["prod"]["base_url"] == "https://api.mangrovedeveloper.ai/api/v1"
    assert path.read_bytes() == before
