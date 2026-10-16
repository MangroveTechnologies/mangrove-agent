"""Authorize Mangrove Agent and save public tool schemas, never account data."""

import asyncio
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.config import app_config
from src.services.robinhood_client import CALLBACK, catalog, session


async def connect():
    loop = asyncio.get_running_loop()
    response = loop.create_future()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            values = parse_qs(parsed.query)
            if parsed.path != "/callback" or not values.get("code") or not values.get("state"):
                self.send_error(400)
                return
            code, state = values["code"][0], values["state"][0]

            def deliver():
                if not response.done():
                    response.set_result((code, state))

            loop.call_soon_threadsafe(deliver)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Return to the Mangrove terminal to check authorization completed.")

        def log_message(self, *_):
            pass  # OAuth codes must never enter logs.

    server = HTTPServer(("127.0.0.1", urlsplit(CALLBACK).port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def redirect(url):
        print("Authorize Mangrove Agent in the Robinhood browser window. No trades will be placed.", flush=True)
        if not webbrowser.open(url):
            print("Open this authorization URL in your private browser:\n" + url, flush=True)

    async def callback():
        return await asyncio.wait_for(response, timeout=300)

    try:
        async with session(redirect_handler=redirect, callback_handler=callback) as client:
            tools = await catalog(client)
            dest = Path(str(app_config.DB_PATH)).parent / "robinhood-tools.json"
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(dest, "w", opener=lambda p, f: os.open(p, f, 0o600)) as out:
                json.dump([t.model_dump(mode="json", by_alias=True) for t in tools.values()], out, indent=2)
            print(f"Mangrove Agent authorized. Discovered {len(tools)} tools; schemas saved to {dest}.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        asyncio.run(connect())
    except KeyboardInterrupt:
        print("Authorization canceled. No trades placed.")
        raise SystemExit(1)
    except Exception:
        print("Authorization did not complete. Check Robinhood and the callback port; no trades placed.")
        raise SystemExit(1)
