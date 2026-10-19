"""The client must never ship a second strategy author or skill library."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_plugin_has_no_copied_server_skills():
    plugin = json.loads((ROOT / '.claude-plugin/plugin.json').read_text())
    for path in plugin['skills']:
        assert (ROOT / path).is_dir()
    for name in ('michael', 'create-strategy', 'custom-signal', 'backtest', 'sieve', 'sweep'):
        assert not (ROOT / '.claude/skills' / name).exists()
    assert not (ROOT / 'scripts/sync-michael-skills.py').exists()


def test_no_local_authoring_implementations_or_advertisements():
    from src.services import strategy_service
    assert not hasattr(strategy_service, 'create_manual')
    assert not hasattr(strategy_service, 'create_autonomous')
    tools = (ROOT / 'server/src/mcp/tools.py').read_text()
    for name in ('agent_create_strategy_manual', 'agent_create_strategy_autonomous',
                 'agent_search_reference_strategies', 'agent_build_strategy_from_reference'):
        assert f'@server.tool(name="{name}")' not in tools
    assert not (ROOT / 'server/src/services/data/reference_strategies.json').exists()


def test_no_local_copies_of_server_rules_or_data():
    """MangroveAI owns timeframes, strategy schemas, trading defaults and backtest
    thresholds. A file mirroring them here drifts from the server unnoticed."""
    for path in ('server/src/shared/timeframes.py',
                 'server/src/services/trading_defaults.py',
                 'server/src/services/data'):
        assert not (ROOT / path).exists(), path
