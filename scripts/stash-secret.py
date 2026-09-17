"""Stash a secret directly from a private terminal prompt into a loopback agent."""
from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
        host = url.hostname
        if host == "localhost":
            host = "127.0.0.1"  # Do not depend on hostname resolution for secrets.
        if (url.scheme not in {"http", "https"} or not host
                or not ipaddress.ip_address(host).is_loopback
                or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return f"{url.scheme}://{authority}/api/v1/agent/wallet/stash-secret"
    except (ValueError, TypeError):
        raise ValueError("LOCAL_AGENT_URL must be an HTTP(S) loopback origin without credentials or a path.") from None


def stash(url, api_key, secret):
    endpoint = loopback_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(endpoint, data=json.dumps({"secret": secret}).encode(),
        method="POST", headers={"Content-Type": "application/json", "X-API-Key": api_key})
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("Agent did not accept the secret.")
            result = json.loads(response.read(16385))
        token, ttl = result.get("vault_token"), result.get("secret_ttl_seconds")
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token)
                or type(ttl) is not int or not 0 < ttl <= 3600):
            raise ValueError("Agent returned invalid vault metadata.")
        return token, ttl
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ValueError(f"Agent refused the secret (HTTP {status}); check local authentication/configuration.") from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError("Could not stash the secret; check the local agent and authentication.") from None


def write_handoff(token, ttl):
    """Store a short-lived bearer capability outside stdout and the checkout."""
    if os.name != "posix":
        raise ValueError("Private handoff files require POSIX permissions.")
    directory = Path(tempfile.mkdtemp(prefix="mangrove-wallet-import-"))
    path = directory / "handoff.json"
    try:
        # The private directory and exclusive file creation prevent symlink reuse.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"vault_token": token, "secret_ttl_seconds": ttl}, output)
            output.write("\n")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        directory.rmdir()
        raise


def main():
    try:
        endpoint = os.environ.get("LOCAL_AGENT_URL", "http://127.0.0.1:9080")
        loopback_url(endpoint)  # Refuse an unsafe destination before prompting.
        config = Path(os.environ["CONFIG_FILE"])
        if os.name == "posix":
            config.chmod(0o600)
        raw = json.loads(config.read_text()).get("API_KEYS", "")
        key = next((str(k).strip() for k in (raw if isinstance(raw, list) else str(raw).split(","))
                    if str(k).strip()), "")
        if not key:
            raise ValueError("No local API key configured; run scripts/setup.sh.")
        if not sys.stdin.isatty():
            raise ValueError("Run this script in an interactive terminal for hidden secret entry.")
        print("Paste a private key or 12/24-word mnemonic. Input is hidden.")
        with warnings.catch_warnings():
            # Never let getpass fall back to echoed stdin.
            warnings.simplefilter("error", getpass.GetPassWarning)
            secret = getpass.getpass("secret: ")
        try:
            if not secret.strip():
                raise ValueError("Empty secret; nothing was sent.")
            token, ttl = stash(endpoint, key, secret)
        finally:
            # Drop this reference; immutable Python strings cannot be securely erased.
            del secret
        handoff = write_handoff(token, ttl)
        print("✓ stashed. A private, single-use import handoff is ready; import promptly.")
        print(f"Handoff file: {handoff}")
        print("Next, tell your local agent: Read the handoff JSON at the path above, "
              "call import_wallet with its vault_token, then delete the file and its "
              "containing temporary directory. Do not display the token.")
        print("If you cancel, delete that file and directory. The token expires server-side.")
        return 0
    except (OSError, KeyError, ValueError, getpass.GetPassWarning):
        print("Could not stash the secret. Check the local config, loopback URL, API key and agent; run from an interactive terminal.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; no import completed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
