"""Robinhood's official MCP transport, with an agent-owned OAuth grant.

No Claude/Codex credential files are read. Background execution never starts
interactive login, and order calls are never automatically retried here.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from src.config import app_config
from src.shared.crypto.fernet import decrypt, encrypt
from src.shared.errors import SdkError
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
CALLBACK = "http://127.0.0.1:8766/callback"


class OAuthStorage:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(str(app_config.DB_PATH)).parent / "robinhood-oauth.enc"

    def _read(self):
        if not self.path.exists():
            return {}
        return json.loads(decrypt(self.path.read_bytes()))

    def _write(self, key, value):
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".robinhood-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypt(json.dumps(data).encode()))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def get_tokens(self):
        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens):
        self._write("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self):
        value = self._read().get("client")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, info):
        self._write("client", info.model_dump(mode="json"))


async def _login_required(*_):
    raise SdkError("Robinhood requires authorization. Run ./scripts/connect-robinhood.sh in a private terminal.")


@asynccontextmanager
async def session(*, redirect_handler=None, callback_handler=None, storage=None):
    protect_mcp_diagnostics()
    store = storage or OAuthStorage()
    if redirect_handler is None and await store.get_tokens() is None:
        await _login_required()
    auth = OAuthClientProvider(
        server_url=ENDPOINT,
        client_metadata=OAuthClientMetadata(
            client_name="Mangrove Agent",
            redirect_uris=[CALLBACK],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=store,
        redirect_handler=redirect_handler or _login_required,
        callback_handler=callback_handler or _login_required,
    )
    async with httpx.AsyncClient(auth=auth, timeout=30, follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(ENDPOINT, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                yield client


async def catalog(client):
    from src.mcp.mangrove_proxy import validate_tool

    result, cursors, cursor = {}, set(), None
    for _ in range(16):
        page = await client.list_tools(cursor=cursor)
        for tool in page.tools:
            validate_tool(tool)
            if tool.name in result or len(result) >= 512:
                raise SdkError("Invalid Robinhood tool catalog.")
            result[tool.name] = tool
        cursor = page.nextCursor
        if not cursor:
            return result
        if cursor in cursors:
            break
        cursors.add(cursor)
    raise SdkError("Robinhood tool catalog pagination did not finish.")


async def invoke(client, tools, name, arguments):
    tool = tools.get(name)
    if tool is None or not Draft202012Validator(tool.inputSchema).is_valid(arguments):
        raise SdkError(f"Robinhood contract mismatch for {name}; refresh the venue contract before execution.")
    result = await client.call_tool(name, arguments)
    if result.isError:
        # Never log arbitrary upstream text (may contain account data).
        raise SdkError(f"Robinhood {name} returned an error. No automatic retry was attempted.")
    if result.structuredContent is not None:
        data = result.structuredContent
        if tool.outputSchema is not None:
            if not Draft202012Validator(tool.outputSchema).is_valid(data):
                raise SdkError(f"Robinhood output contract mismatch for {name}.")
        return data
    texts = [c.text for c in result.content if c.type == "text"]
    if len(texts) == 1:
        try:
            data = json.loads(texts[0])
            if tool.outputSchema is not None:
                if not Draft202012Validator(tool.outputSchema).is_valid(data):
                    raise SdkError(f"Robinhood output contract mismatch for {name}.")
            return data
        except ValueError:
            pass
    raise SdkError(f"Robinhood {name} returned an unsupported response shape.")
