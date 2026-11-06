"""Dedicated-UID narrow signing service. No administrator RPCs exist."""
from __future__ import annotations

import argparse
import inspect
import signal
import socket
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


def serve(settings):
    path = Path(settings["socket_path"])
    if path.exists() or path.is_symlink():
        # Do not unlink an existing daemon's endpoint or arbitrary files.
        raise ValueError("Custody socket already exists; stop the old service and remove its stale socket as the administrator")
    broker = Broker(settings)
    def terminate(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        # Traversal is read-only to the agent; peer UID authenticates every call.
        path.chmod(0o666)
        listener.listen(16)
        try:
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(10)
                    try:
                        if peer_uid(connection) != settings["agent_uid"]:
                            raise ValueError("Unauthorized custody peer")
                        result = broker.dispatch(receive(connection))
                        send(connection, {"ok": True, "result": result})
                    except Exception:
                        # Never reflect a request, signature, internal exception or secret.
                        try:
                            send(connection, {"ok": False, "error": "Custody operation refused"})
                        except (OSError, ValueError):
                            pass
        finally:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    settings = initialize(args.config)
    serve(settings)


if __name__ == "__main__":
    main()
