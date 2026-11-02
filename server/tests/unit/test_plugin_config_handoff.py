"""Exercise the launcher's real config handoff without installing or starting an agent."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("existing", [False, True])
def test_plugin_config_key_uses_stdin_and_never_process_arguments(tmp_path, existing):
    root = tmp_path / "plugin cache with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "server/src/config").mkdir(parents=True)
    example = root / "server/src/config/local-example-config.json"
    example.write_text(json.dumps({"MANGROVEMARKETS_BASE_URL": "http://localhost:9081"}))
    helper = root / "scripts/plugin_seed_config.py"
    helper.write_bytes((ROOT / "scripts/plugin_seed_config.py").read_bytes())
    config = tmp_path / "private config.json"
    if existing:
        config.write_text(json.dumps({"custom": {"preserved": True},
                                     "MANGROVEMARKETS_BASE_URL": "https://chosen.example"}))
    argv_log = tmp_path / "child-argv.json"
    venv = tmp_path / "plugin data/venv"
    (venv / "bin").mkdir(parents=True)
    wrapper = venv / "bin/python"
    wrapper.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
Path(os.environ["ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]))
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""")
    wrapper.chmod(0o755)
    launcher = (ROOT / "scripts/plugin-start-agent.sh").read_text()
    config_section = launcher.split("# -- 2. config ", 1)[1].split("\n", 1)[1].split("# -- 3. (re)start ", 1)[0]
    key = "fixture key with spaces"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", config_section],
        env={"PATH": os.environ["PATH"], "ROOT": str(root), "VENV": str(venv),
             "CFG": str(config), "KEY": key, "PORT": "19080",
             "MARKETS_URL_DEFAULT": "https://markets.example", "ARGV_LOG": str(argv_log)},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(key not in arg for arg in json.loads(argv_log.read_text()))
    assert key not in result.stdout + result.stderr
    saved = json.loads(config.read_text())
    assert saved["MANGROVE_API_KEY"] == saved["API_KEYS"] == key
    assert saved["LOCAL_AGENT_URL"] == "http://127.0.0.1:19080"
    assert saved["DB_PATH"] == "./agent-data/agent.db"
    assert saved["MASTER_KEY_PATH"] == "./agent-data/master.key"
    assert saved["MANGROVEMARKETS_BASE_URL"] == (
        "https://chosen.example" if existing else "https://markets.example")
    if existing:
        assert saved["custom"] == {"preserved": True}
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_plugin_config_missing_stdin_does_not_replace_a_saved_key(tmp_path):
    example = tmp_path / "example.json"
    example.write_text("{}")
    config = tmp_path / "config.json"
    original = '{"MANGROVE_API_KEY": "fixture key"}'
    config.write_text(original)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/plugin_seed_config.py"),
         str(example), str(config), "19080", "https://markets.example"],
        input="", capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert config.read_text() == original
    assert "fixture key" not in result.stdout + result.stderr
