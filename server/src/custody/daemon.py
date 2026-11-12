"""Dedicated-UID narrow signing service. No administrator RPCs exist."""
from __future__ import annotations

import argparse
import fcntl
import inspect
import os
import signal
import socket
import threading
from pathlib import Path

from src.custody.bootstrap import initialize
from src.custody.policy import Policy, digest
from src.custody.transport import peer_uid, receive, send


class Broker:
    def __init__(self, settings):
        self.settings = settings
        self.policy = Policy(settings.get("policy", {}), Path(settings["state_dir"]))

    def dispatch(self, request):
        from src.services import wallet_manager

        if set(request) != {"operation", "arguments"} or not isinstance(request["arguments"], dict):
            raise ValueError("Invalid custody request")
        operation, arguments = request["operation"], request["arguments"]
        if operation == "list_wallets" and not arguments:
            return [item.model_dump(mode="json") for item in wallet_manager.list_wallets()]
        if operation == "create_wallet":
            if set(arguments) - {"chain", "network", "chain_id", "label"}:
                raise ValueError("Unexpected wallet creation arguments")
            if arguments.get("chain") not in {"evm", "xrpl"}:
                raise ValueError("Unsupported wallet chain")
            if len(wallet_manager.list_wallets()) >= self.settings.get("max_wallets", 16):
                raise ValueError("Custody wallet creation limit reached")
            item = wallet_manager.create_wallet(**arguments).model_dump(mode="json")
            self.policy._record(operation, digest(request), "created")
            self.policy.db.commit()
            return item
        allowed = {"sign", "sign_x402_authorization", "sign_siwx_message", "sign_marketplace_proof"}
        if operation not in allowed:
            raise ValueError("Operation is not available to the agent")
        inspect.signature(getattr(wallet_manager, operation)).bind(**arguments)
        # Deterministic metadata preconditions do not create a signature and
        # must not consume a nonce or budget. A later human backup can then
        # retry the same operation normally.
        if operation in {"sign", "sign_x402_authorization", "sign_marketplace_proof"}:
            wallet_manager.require_backup_confirmed(arguments["wallet_address"])
        row = wallet_manager._get_wallet_row(arguments["wallet_address"])
        if not row or (operation != "sign_marketplace_proof" and row["chain"] != "evm"):
            raise ValueError("Signing wallet has the wrong chain adapter")

        def perform():
            forwarded = dict(arguments)
            if operation == "sign":
                from src.services.transaction_intent import intent_from_dict
                forwarded["intent"] = intent_from_dict(forwarded["intent"])
            result = getattr(wallet_manager, operation)(**forwarded)
            return "0x" + result.hex() if isinstance(result, bytes) else result

        return self.policy.sign(operation, arguments, perform)


MAX_CONNECTIONS = 8
READ_DEADLINE_SECONDS = 10


def _acquire_state_lock(state_dir: Path) -> int:
    """Hold an exclusive lock on the state directory for the daemon's lifetime.

    The startup reservation sweep is safe only when no other daemon can be
    signing from the same ledger, so a second daemon refuses to start.
    """
    descriptor = os.open(state_dir / "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise ValueError("A custody daemon is already running for this state directory") from None
    return descriptor


def _release_state_lock(descriptor: int, dispatch_lock: threading.Lock) -> None:
    """Release the state directory lock once no request can still sign.

    Taking ``dispatch_lock`` and keeping it waits for a request that is
    reserving, signing or journaling, and stops any later request from
    starting. Every reservation this daemon made then has its signature stored
    or has been refunded, so a successor's startup sweep cannot refund a
    signature that is about to leave custody.
    """
    dispatch_lock.acquire()
    os.close(descriptor)


def _handle(connection, broker, agent_uid, dispatch_lock, slots):
    """Read, authorize and answer one client; only dispatch is serialized."""
    try:
        with connection:
            connection.settimeout(READ_DEADLINE_SECONDS)
            try:
                if peer_uid(connection) != agent_uid:
                    raise ValueError("Unauthorized custody peer")
                request = receive(connection, timeout_seconds=READ_DEADLINE_SECONDS)
                with dispatch_lock:
                    result = broker.dispatch(request)
                send(connection, {"ok": True, "result": result})
            except Exception:
                # Never reflect a request, signature, internal exception or secret.
                try:
                    send(connection, {"ok": False, "error": "Custody operation refused"})
                except (OSError, ValueError):
                    pass  # The client disconnected or stalled; it gets no response.
    finally:
        slots.release()


def serve(settings):
    path = Path(settings["socket_path"])
    if path.exists() or path.is_symlink():
        # Do not unlink an existing daemon's endpoint or arbitrary files.
        raise ValueError("Custody socket already exists; stop the old service and remove its stale socket as the administrator")
    state_lock = _acquire_state_lock(Path(settings["state_dir"]))
    # Each client reads its request in its own thread, so a silent or slow
    # client holds one slot until its read deadline instead of blocking the
    # service. Policy and signing still run one request at a time.
    dispatch_lock = threading.Lock()
    try:
        broker = Broker(settings)
        broker.policy.recover_unfinished()
        def terminate(_signum, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            # Traversal is read-only to the agent; peer UID authenticates every call.
            path.chmod(0o666)
            listener.listen(16)
            try:
                while True:
                    connection, _ = listener.accept()
                    if not slots.acquire(blocking=False):
                        with connection:
                            connection.settimeout(1)
                            try:
                                send(connection, {"ok": False, "error": "Custody service is busy"})
                            except (OSError, ValueError):
                                pass  # A refused client that does not read gets nothing.
                        continue
                    threading.Thread(target=_handle, args=(connection, broker, settings["agent_uid"], dispatch_lock, slots),
                                     daemon=True).start()
            finally:
                path.unlink(missing_ok=True)
    finally:
        _release_state_lock(state_lock, dispatch_lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    settings = initialize(args.config)
    serve(settings)


if __name__ == "__main__":
    main()
