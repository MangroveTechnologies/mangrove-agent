"""Human backup uses a validated controlling TTY, never generic stdout."""
import errno
import os
import select
import signal
import stat
import sys
import time
from types import SimpleNamespace

import pytest
from src.custody import runtime, terminal
from src.services import wallet_manager


@pytest.mark.parametrize("failure", ["no_terminal", "regular_file", "not_tty", "background", "cancelled"])
def test_backup_refuses_before_key_access_and_closes_descriptor(monkeypatch, failure):
    monkeypatch.setattr(runtime, "_active", True)
    monkeypatch.setattr(wallet_manager, "reveal_wallet_secret", lambda *_: pytest.fail("Refused backup loaded a key"))
    closed, writes = [], []
    def open_terminal(*_):
        if failure == "no_terminal":
            raise OSError(errno.ENXIO, "No controlling terminal")
        return 17
    monkeypatch.setattr(terminal.os, "open", open_terminal)
    monkeypatch.setattr(terminal.os, "close", closed.append)
    monkeypatch.setattr(terminal.os, "fstat", lambda *_: SimpleNamespace(st_mode=stat.S_IFREG if failure == "regular_file" else stat.S_IFCHR))
    monkeypatch.setattr(terminal.os, "isatty", lambda *_: failure != "not_tty")
    monkeypatch.setattr(terminal.os, "tcgetpgrp", lambda *_: 20 if failure == "background" else 10)
    monkeypatch.setattr(terminal.os, "getpgrp", lambda: 10)
    monkeypatch.setattr(terminal.os, "read", lambda *_: b"CANCEL\n")
    monkeypatch.setattr(terminal, "_write_terminal", lambda descriptor, value: writes.append((descriptor, value)))
    with pytest.raises((OSError, ValueError)):
        terminal.display_wallet_backup("public-wallet-address")
    assert closed == ([] if failure == "no_terminal" else [17])
    assert not writes if failure != "cancelled" else all(descriptor == 17 for descriptor, _ in writes)


def test_backup_requires_custody_role_before_opening_a_terminal(monkeypatch):
    monkeypatch.setattr(runtime, "_active", False)
    monkeypatch.setattr(terminal.os, "open", lambda *_: pytest.fail("Unprivileged role opened backup terminal"))
    with pytest.raises(Exception, match="isolated custody service"):
        terminal.display_wallet_backup("public-wallet-address")


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Custody supports POSIX controlling terminals")
def test_real_synthetic_backup_goes_only_to_controlling_pty():
    import pty

    from eth_account import Account

    account = Account.create()  # Unfunded, generated only for this local test.
    synthetic_secret = "0x" + account.key.hex().removeprefix("0x")
    output_read, output_write = os.pipe()
    child, master = pty.fork()
    if child == 0:
        os.close(output_read)
        os.dup2(output_write, sys.stdout.fileno())
        os.dup2(output_write, sys.stderr.fileno())
        os.close(output_write)
        runtime._active = True
        wallet_manager.reveal_wallet_secret = lambda _: SimpleNamespace(secret=synthetic_secret)
        # The forked child must always leave through os._exit, never return into pytest.
        status = 1
        try:
            terminal.display_wallet_backup(account.address)
            status = 0
        finally:
            os._exit(status)
    os.close(output_write)
    received, confirmed = bytearray(), False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            received.extend(chunk)
            if not confirmed and b"Type DISPLAY SECRET" in received:
                os.write(master, b"DISPLAY SECRET\n")
                confirmed = True
        else:
            os.kill(child, signal.SIGKILL)
            pytest.fail("Synthetic backup did not finish")
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert synthetic_secret.encode() in received
        assert os.read(output_read, 4096) == b""  # No stdout/stderr secret or logging.
    finally:
        os.close(output_read)
        os.close(master)
