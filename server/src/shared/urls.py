"""URL helpers shared by anything that persists or prints a URL.

Lives here rather than beside its first caller because it has two unrelated
consumers with the same requirement: the spend ledger, which writes URLs to
a durable file on disk, and the payer, which puts them in log lines and in
error messages that reach the conversation transcript.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def strip_query(url: str | None) -> str | None:
    """Return `url` without its query string or fragment.

    Query strings are where credentials end up when something goes wrong
    upstream -- a token appended to a URL by a misconfigured client, a
    signed link, an API key someone pasted into a base URL. Nothing that
    outlives the request should carry one, which covers the database, the
    log file, and any error text shown to a user.

    Returns None for a URL that is empty or unparseable, so a caller storing
    the result records "no resource" rather than a half-parsed string.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) or None
