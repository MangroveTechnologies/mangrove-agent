"""Authorize Mangrove Agent and save public tool schemas, never account data."""

import asyncio
import json
import os
import select
import sys
import threading
import webbrowser
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.config import app_config
from src.services.robinhood_client import CALLBACK, catalog, session


def show_pending_menu():
    # A single available action needs no navigation; Enter selects it. Keep
    # the layout consistent with setup, without blocking OAuth completion.
    try:
        "◆│●".encode(sys.stdout.encoding or "ascii")
        symbols = ("◆", "│", "●")
    except UnicodeEncodeError:
        symbols = ("?", "|", ">")
    title, bar, selected = symbols
    if sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb" and "NO_COLOR" not in os.environ:
        selected = f"\x1b[34m{selected}\x1b[0m"
    print(f"{title}  Complete Robinhood login in your browser\n"
          f"{bar}  {selected} Skip for now", flush=True)


async def wait_for_skip():
    """Read terminal input without leaving a blocking reader after OAuth finishes."""
    pending = ""
    while True:
        if os.name == "nt":
            import msvcrt

            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in {"\r", "\n"}:
                    print(flush=True)
                    if pending.strip().lower() in {"", "skip", "1"}:
                        return
                    pending = ""
                elif char == "\b":
                    pending = pending[:-1]
                elif char.isprintable():
                    pending += char
                    print(char, end="", flush=True)
        elif select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if not line or line.strip().lower() in {"", "skip", "1"}:
                return
            print("Press Enter to skip.", flush=True)
        await asyncio.sleep(0.1)


async def run_connection():
    if not sys.stdin.isatty():
        await connect()
        return 0
    show_pending_menu()
    connection = asyncio.create_task(connect())
    skip = asyncio.create_task(wait_for_skip())
    try:
        done, _ = await asyncio.wait({connection, skip}, return_when=asyncio.FIRST_COMPLETED)
        # Prefer a completed connection if login and terminal input race.
        if connection in done:
            await connection
            return 0
        await skip
        connection.cancel()
        with suppress(asyncio.CancelledError):
            await connection
        return 2
    finally:
        for task in (connection, skip):
            task.cancel()
        await asyncio.gather(connection, skip, return_exceptions=True)


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
        if not webbrowser.open(url):
            print("Open this authorization URL in your private browser:\n" + url, flush=True)
            if sys.stdin.isatty():
                show_pending_menu()

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
        raise SystemExit(asyncio.run(run_connection()))
    except KeyboardInterrupt:
        print("Authorization canceled. No trades placed.")
        raise SystemExit(1)
    except Exception:
        print("Authorization did not complete. Check Robinhood and the callback port; no trades placed.")
        raise SystemExit(1)
