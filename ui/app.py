"""Chainlit chat UI for Mangrove Agent — Sage persona.

Routes through the local `claude` CLI subprocess instead of calling the
Anthropic API directly. No separate API key required — uses whatever
OAuth auth the user already has from Claude Code.

Claude Code picks up CLAUDE.md automatically (Sage persona + all rules)
and the registered MCP server (all tools) since we run from the repo root.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import AsyncGenerator

import chainlit as cl

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Claude CLI streaming
# ---------------------------------------------------------------------------

Event = tuple[str, object]  # (event_type, payload)


async def _stream(user_message: str, session_id: str | None) -> AsyncGenerator[Event, None]:
    """
    Runs `claude -p <message> --output-format stream-json` as a subprocess
    and yields parsed events:
      ("text",       str)                     — text chunk to stream
      ("tool_start", {id, name, input})        — tool call beginning
      ("tool_end",   {id, name, input, output, is_error})  — tool result
      ("session_id", str)                      — session to resume next turn
      ("error",      str)                      — fatal error message
    """
    api_key = os.getenv("MANGROVE_AGENT_API_KEY", "dev-key-1")
    cmd = [
        "claude", "-p", user_message,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", MODEL,
        "--append-system-prompt",
        (
            f'For every tool call that has an api_key parameter, always pass api_key="{api_key}". '
            "When thinking out loud between tool calls, write each status update as its own "
            "short paragraph separated by a blank line — never run multiple updates together "
            "on the same line. Keep each update to one or two sentences."
        ),
    ]
    if session_id:
        cmd += ["--resume", session_id]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=REPO_ROOT,
            limit=10 * 1024 * 1024,  # 10 MB — tool responses can be large
        )
    except FileNotFoundError:
        yield ("error", (
            "`claude` CLI not found. Install Claude Code at https://claude.ai/code "
            "and make sure it is on your PATH."
        ))
        return

    # Track tool_start events so we can pair them with tool_end
    pending: dict[str, dict] = {}

    async for raw in proc.stdout:
        line = raw.decode().strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Every event carries session_id — grab the latest
        if sid := ev.get("session_id"):
            yield ("session_id", sid)

        etype = ev.get("type", "")

        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    yield ("text", block["text"])
                elif btype == "tool_use":
                    tool_id = block.get("id", "")
                    pending[tool_id] = {
                        "id": tool_id,
                        "name": block.get("name", "tool"),
                        "input": block.get("input", {}),
                    }
                    yield ("tool_start", pending[tool_id])

        elif etype == "tool":
            tool_id = ev.get("tool_use_id", "")
            start = pending.pop(tool_id, {})
            yield ("tool_end", {
                "id": tool_id,
                "name": ev.get("tool_name") or start.get("name", "tool"),
                "input": start.get("input", ev.get("input", {})),
                "output": ev.get("output", ""),
                "is_error": ev.get("is_error", False),
            })

        elif etype == "result" and ev.get("subtype") == "error_during_execution":
            yield ("error", ev.get("error", "Claude returned an error."))

    rc = await proc.wait()
    if rc != 0:
        stderr = (await proc.stderr.read()).decode().strip()
        if stderr:
            yield ("error", stderr)


# ---------------------------------------------------------------------------
# Chainlit handlers
# ---------------------------------------------------------------------------

@cl.set_starters
async def set_starters():
    return [
        cl.Starter(label="Pick a strategy for me",  message="Pick a strategy for me for ETH"),
        cl.Starter(label="Show my strategies",      message="Show my active strategies and their status"),
        cl.Starter(label="Check balances",          message="Show my wallet balances"),
        cl.Starter(label="Available signals",       message="What signals are available?"),
    ]


async def _respond(prompt: str) -> None:
    """Stream a Claude response for `prompt`, updating session state."""
    session_id: str | None = cl.user_session.get("session_id")
    new_session_id = session_id

    response_msg = cl.Message(content="", author="Sage")
    await response_msg.send()

    pending_steps: dict[str, dict] = {}
    after_tool = False
    last_char = ""  # last character of the previous text chunk

    async for etype, data in _stream(prompt, session_id):
        if etype == "session_id":
            new_session_id = data

        elif etype == "text":
            if after_tool:
                await response_msg.stream_token("\n\n")
                after_tool = False
                last_char = ""
            # Inject a paragraph break when a new sentence starts right after
            # a period with no gap (e.g. "winner.Searching")
            elif last_char == "." and data and data[0].isupper():
                await response_msg.stream_token("\n\n")
            # Also break on ". Capital" patterns within the chunk itself
            data = re.sub(r'\. ([A-Z])', r'.\n\n\1', data)
            await response_msg.stream_token(data)
            last_char = data[-1] if data else last_char

        elif etype == "tool_start":
            pending_steps[data["id"]] = data

        elif etype == "tool_end":
            start = pending_steps.pop(data["id"], {})
            raw_name = data.get("name") or start.get("name", "tool")
            display_name = raw_name.split("__")[-1].replace("_", " ").title()
            async with cl.Step(name=display_name, type="tool") as step:
                step.input = start.get("input", data.get("input", {}))
                step.output = data.get("output", "")
            after_tool = True

        elif etype == "error":
            await cl.Message(content=f"**Error:** {data}", author="Sage").send()

    await response_msg.update()
    cl.user_session.set("session_id", new_session_id)


@cl.on_chat_start
async def on_chat_start():
    if not shutil.which("claude"):
        await cl.Message(
            content=(
                "**Setup issue:** `claude` CLI not found on PATH.\n\n"
                "Install Claude Code at https://claude.ai/code, authenticate with "
                "`claude auth login`, then restart this UI."
            ),
            author="Sage",
        ).send()
        return

    cl.user_session.set("session_id", None)


@cl.on_message
async def on_message(message: cl.Message):
    await _respond(message.content)
