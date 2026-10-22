"""Print the plugin session context with the plugin root and agent home filled in.

Usage: plugin_render_context.py CONTEXT_FILE PLUGIN_ROOT AGENT_HOME
Called by plugin-bootstrap.sh (SessionStart hook).
"""
import sys

with open(sys.argv[1]) as f:
    text = f.read()
print(text.replace("${CLAUDE_PLUGIN_ROOT}", sys.argv[2]).replace("${MANGROVE_AGENT_HOME}", sys.argv[3]))
