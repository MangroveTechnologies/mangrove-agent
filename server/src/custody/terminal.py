"""Explicit human backup display to the custody process's controlling terminal.

This is an intentional secret-export sink, not application logging. OS-isolated
custody and the separately controlled administrator session supply authority.
A TTY alone cannot identify a human or prevent an administrator recording it.
"""
from __future__ import annotations

import os
import stat

from src.custody import runtime


def _write_terminal(descriptor: int, text: str) -> None:
    pending = memoryview(text.encode("utf-8"))
    while pending:
        written = os.write(descriptor, pending)
        if written <= 0:
            raise OSError("Custody terminal write failed")
        pending = pending[written:]


def display_wallet_backup(address: str) -> None:
    """Confirm and display directly on /dev/tty; never return or log the key."""
    runtime.require()
    descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if (not stat.S_ISCHR(os.fstat(descriptor).st_mode)
                or not os.isatty(descriptor)
                or os.tcgetpgrp(descriptor) != os.getpgrp()):
            raise ValueError("Wallet backup requires the foreground controlling administrator terminal")
        _write_terminal(descriptor, "Wallet: " + address + "\n")
        _write_terminal(descriptor, "Type DISPLAY SECRET to show this wallet's backup in this private terminal: ")
        if os.read(descriptor, 64).rstrip(b"\r\n") != b"DISPLAY SECRET":
            raise ValueError("Backup cancelled")
        from src.services import wallet_manager

        # Deliberate human-only export: load only after the protected role,
        # controlling-terminal checks and confirmation on that same descriptor.
        # This descriptor cannot be redirected through stdout or logging sinks.
        secret = wallet_manager.reveal_wallet_secret(address).secret
        try:
            _write_terminal(descriptor, secret + "\n")
        finally:
            # Python immutable strings cannot be reliably erased.
            del secret
        _write_terminal(descriptor, "Save it outside the agent, then use confirm-backup. Clear terminal scrollback.\n")
    finally:
        os.close(descriptor)
