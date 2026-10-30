#!/usr/bin/env python3
"""Small stdlib-only probes used by the Claude plugin lifecycle scripts."""

import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_status(url, api_key=None):
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("X-API-Key", api_key)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (OSError, ValueError, urllib.error.URLError):
        return 0


def healthy_agent(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=2) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(64 * 1024))
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "healthy"
        and payload.get("scheduler_running") is True
    )


def plugin_process(pid, root_file):
    """Return true only for this plugin's uvicorn process and recorded root."""
    try:
        pid_value = int(pid)
        if pid_value < 1:
            return False
        root = Path(root_file).read_text(encoding="utf-8").strip()
        if not root:
            return False
        result = subprocess.run(
            ["ps", "-p", str(pid_value), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        arguments = shlex.split(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    app_dir = os.path.join(root, "server")
    return (
        result.returncode == 0
        and arguments
        and "python" in Path(arguments[0]).name.lower()
        and len(arguments) > 2
        and arguments[1:3] == ["-m", "uvicorn"]
        and "src.app:app" in arguments
        and "--app-dir" in arguments
        and arguments[arguments.index("--app-dir") + 1] == app_dir
    )


def owned_healthy_agent(url, pid_file, root_file, expected_root):
    try:
        pid = Path(pid_file).read_text(encoding="utf-8").strip()
        recorded_root = Path(root_file).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return (
        recorded_root == expected_root
        and plugin_process(pid, root_file)
        and healthy_agent(url)
    )


def main(argv):
    if len(argv) == 2 and argv[0] == "--healthy":
        return 0 if healthy_agent(argv[1]) else 1
    if len(argv) == 3 and argv[0] == "--plugin-process":
        return 0 if plugin_process(argv[1], argv[2]) else 1
    if len(argv) == 5 and argv[0] == "--owned-health":
        return 0 if owned_healthy_agent(*argv[1:]) else 1
    if len(argv) in (1, 2):
        print(http_status(argv[0], argv[1] if len(argv) == 2 else None))
        return 0
    print(
        "Usage: plugin_http_status.py URL [API_KEY] | --healthy URL | "
        "--plugin-process PID ROOT_FILE | "
        "--owned-health URL PID_FILE ROOT_FILE EXPECTED_ROOT",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
