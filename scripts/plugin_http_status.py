"""Print the HTTP status code for a URL (0 when unreachable).

Usage: plugin_http_status.py URL [API_KEY]
Called by plugin-bootstrap.sh and plugin-start-agent.sh.
"""
import sys
import urllib.error
import urllib.request

req = urllib.request.Request(sys.argv[1])
if len(sys.argv) > 2:
    req.add_header("X-API-Key", sys.argv[2])
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
