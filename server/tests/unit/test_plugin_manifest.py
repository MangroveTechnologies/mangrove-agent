"""The Claude Code plugin + marketplace manifests stay installable.

`/plugin marketplace add MangroveTechnologies/mangrove-agent` reads
.claude-plugin/marketplace.json and plugin.json at the repo root. A renamed
skill folder, a non-executable hook script, or a hook matcher on the wrong MCP
namespace breaks installs silently, so pin them here.
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / ".claude-plugin"
DEV_ONLY_SKILLS = {"tool-spec", "check-alignment", "audit-security"}


def _load(name: str) -> dict:
    return json.loads((PLUGIN_DIR / name).read_text())


def _hook_entries():
    plugin = _load("plugin.json")
    hooks = json.loads((ROOT / plugin["hooks"]).read_text())["hooks"]
    for event, groups in hooks.items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, group.get("matcher"), hook["command"]


def test_marketplace_lists_this_repo_root_as_the_plugin():
    marketplace = _load("marketplace.json")
    plugin = _load("plugin.json")
    entries = [p for p in marketplace["plugins"] if p["name"] == plugin["name"]]
    assert entries, "marketplace.json must list the plugin by its plugin.json name"
    assert entries[0]["source"] == "./"


def test_every_shipped_skill_exists_and_is_named_for_its_folder():
    for rel in _load("plugin.json")["skills"]:
        skill_md = ROOT / rel / "SKILL.md"
        assert skill_md.is_file(), f"missing {skill_md}"
        frontmatter = skill_md.read_text().split("---")[1]
        assert re.search(rf"^name:\s*{re.escape(Path(rel).name)}\s*$", frontmatter, re.M), rel


def test_contributor_only_skills_are_not_shipped():
    shipped = {Path(p).name for p in _load("plugin.json")["skills"]}
    assert not shipped & DEV_ONLY_SKILLS


def test_hook_scripts_exist_and_are_executable():
    for event, _, command in _hook_entries():
        match = re.match(r'"\$\{CLAUDE_PLUGIN_ROOT\}"(/\S+)', command)
        assert match, f"{event}: hook must run a script under ${{CLAUDE_PLUGIN_ROOT}}: {command}"
        script = ROOT / match.group(1).lstrip("/")
        assert script.is_file(), f"{event}: {script} missing"
        assert os.access(script, os.X_OK), f"{event}: {script} not executable"


def test_tool_matchers_use_the_plugin_mcp_namespace():
    plugin = _load("plugin.json")
    prefixes = tuple(f"mcp__plugin_{plugin['name']}_{server}__" for server in plugin["mcpServers"])
    for event, matcher, _ in _hook_entries():
        if matcher and matcher.startswith("mcp__"):
            assert matcher.startswith(prefixes), f"{event}: {matcher}"


def test_mcp_headers_only_reference_declared_user_config():
    plugin = _load("plugin.json")
    declared = set(plugin.get("userConfig", {}))
    for name, server in plugin["mcpServers"].items():
        assert server["type"] in {"http", "sse", "stdio", "ws"}, name
        for value in server.get("headers", {}).values():
            for key in re.findall(r"\$\{user_config\.([A-Za-z0-9_]+)\}", value):
                assert key in declared, f"{name}: header uses undeclared user_config.{key}"


def test_mcp_server_is_loopback_only():
    for server in _load("plugin.json")["mcpServers"].values():
        if server["type"] == "http":
            assert server["url"].startswith("http://127.0.0.1:"), server["url"]


def test_preflight_swap_hook_recognizes_the_plugin_tool_name():
    text = (ROOT / ".claude" / "hooks" / "preflight-swap.sh").read_text()
    assert "mcp__plugin_mangrove-agent_mangrove-agent__execute_swap" in text


def test_session_context_is_shipped():
    assert (PLUGIN_DIR / "session-context.md").is_file()
