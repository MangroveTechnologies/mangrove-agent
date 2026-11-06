"""Human-only custody administration. This module has no network listener."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sqlite3
import sys
import warnings
from pathlib import Path

from src.custody.bootstrap import initialize
from src.custody.policy import Policy, digest
from src.custody.transport import check_ancestors, check_owned_path


def require_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Custody administration requires the independent human administrator's terminal")
    # A TTY alone is not human authentication. The OS identity and separately
    # controlled privilege transition into this UID are the authority.


def migrate(legacy_db: Path, legacy_key: Path, settings: dict) -> dict:
    """Reencrypt a protected stopped-installation snapshot; never return plaintext."""
    import os

    from cryptography.fernet import Fernet

    from src.services.wallet_manager import _derive_address, _xrpl_wallet
    from src.shared.crypto.fernet import encrypt
    from src.shared.db.sqlite import get_connection

    for path in (legacy_db, legacy_key):
        check_owned_path(path, owner=os.geteuid(), private=True)
        check_ancestors(path.parent, allowed_owners={0, os.geteuid()})
    original = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    original.row_factory = sqlite3.Row
    rows = original.execute("SELECT * FROM wallets").fetchall()
    original.close()
    source_key = Fernet(legacy_key.read_bytes().strip())
    prepared, receipt = [], []
    for row in rows:
        if row["encryption_method"] != "fernet-v1":
            raise ValueError("Unexpected legacy wallet encryption; no migration was committed")
        plaintext = source_key.decrypt(bytes(row["encrypted_secret"])).decode()
        try:
            derived = _xrpl_wallet(plaintext).classic_address if row["chain"] == "xrpl" else _derive_address(plaintext)
            if derived.lower() != row["address"].lower():
                raise ValueError("Legacy wallet identity mismatch")
            prepared.append((dict(row), encrypt(plaintext.encode())))
            receipt.append({"address": row["address"], "ciphertext_sha256": hashlib.sha256(bytes(row["encrypted_secret"])).hexdigest()})
        finally:
            del plaintext
    result = {"version": 1, "wallets": receipt}
    receipt_path = Path(settings["state_dir"]) / "migration-receipt.json"
    if receipt_path.exists() and json.loads(receipt_path.read_text()) != result:
        raise ValueError("A different migration receipt exists; no wallet mutation was committed")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row, ciphertext in prepared:
            previous = conn.execute("SELECT * FROM wallets WHERE address=?", (row["address"],)).fetchone()
            if previous:
                # Resume a crash after database commit but before receipt rename.
                # Verify the existing encrypted key and public identity instead of
                # overwriting an already migrated wallet or generating a new one.
                from src.services.wallet_manager import _load_secret
                current = _load_secret(row["address"])
                try:
                    actual = _xrpl_wallet(current).classic_address if previous["chain"] == "xrpl" else _derive_address(current)
                    if (actual.lower() != row["address"].lower() or previous["chain"] != row["chain"]
                            or previous["network"] != row["network"]):
                        raise ValueError("Existing custody wallet does not match migration")
                finally:
                    del current
                continue
            row["encrypted_secret"] = ciphertext
            columns = [name for name in ("id", "address", "chain", "network", "chain_id", "encrypted_secret",
                                         "encryption_method", "label", "created_at", "metadata_json", "backup_confirmed_at") if name in row]
            conn.execute("INSERT INTO wallets(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(row[name] for name in columns))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # The receipt is protected by the custody UID and contains no key/ciphertext.
    temporary = receipt_path.with_suffix(".pending")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, receipt_path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("backup", "confirm-backup"):
        item = sub.add_parser(action)
        item.add_argument("address")
    item = sub.add_parser("import")
    item.add_argument("--chain", choices=["evm", "xrpl"], default="evm")
    item.add_argument("--network", choices=["testnet", "mainnet", "devnet"], default="testnet")
    item.add_argument("--chain-id", type=int, default=84532)
    item.add_argument("--label")
    item = sub.add_parser("migrate")
    item.add_argument("--legacy-db", type=Path, required=True)
    item.add_argument("--legacy-master-key", type=Path, required=True)
    item = sub.add_parser("approve-marketplace")
    item.add_argument("request_file", type=Path)
    args = parser.parse_args()
    require_terminal()
    settings = initialize(args.config)
    from src.services import wallet_manager

    if args.action == "backup":
        print("Wallet: " + args.address)
        if input("Type DISPLAY SECRET to show this wallet's backup in this private terminal: ") != "DISPLAY SECRET":
            raise ValueError("Backup cancelled")
        print(wallet_manager.reveal_wallet_secret(args.address).secret)
        print("Save it outside the agent, then use confirm-backup. Clear terminal scrollback.")
    elif args.action == "confirm-backup":
        print("Wallet: " + args.address)
        if input("Type BACKED UP only after securely saving the secret: ") != "BACKED UP":
            raise ValueError("Backup confirmation cancelled")
        wallet_manager.confirm_backup(args.address)
        print("Backup confirmed; no payment was made.")
    elif args.action == "import":
        if input("Type BACKED UP only if this wallet secret is already saved outside the agent: ") != "BACKED UP":
            raise ValueError("Import cancelled; independent backup was not confirmed")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            secret = getpass.getpass("Wallet secret (hidden, never paste in chat): ")
        try:
            token = wallet_manager.stash_external_secret(secret)
        finally:
            del secret
        result = wallet_manager.import_wallet(token, chain=args.chain, network=args.network,
                                              chain_id=args.chain_id, label=args.label)
        print("Imported wallet: " + result.address)
    elif args.action == "migrate":
        if input("Type MIGRATE STOPPED SNAPSHOT after stopping the legacy agent and protecting its recovery copies: ") != "MIGRATE STOPPED SNAPSHOT":
            raise ValueError("Migration cancelled")
        result = migrate(args.legacy_db, args.legacy_master_key, settings)
        print("Migrated " + str(len(result["wallets"])) + " wallets. Original snapshots and any other backups still contain recoverable secrets.")
    elif args.action == "approve-marketplace":
        request = json.loads(args.request_file.read_text())
        if request.get("operation") != "sign_marketplace_proof" or set(request) != {"operation", "arguments"}:
            raise ValueError("Expected a complete marketplace proof request")
        print(json.dumps(request, indent=2, sort_keys=True))
        if input("Type APPROVE EXACT ACTION after checking every displayed wallet, action and argument: ") != "APPROVE EXACT ACTION":
            raise ValueError("Approval cancelled")
        policy = Policy(settings.get("policy", {}), Path(settings["state_dir"]))
        import time
        policy.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?)", (digest(request), int(time.time())))
        policy.db.commit()
        print("Exact marketplace proof approved for ten minutes. No signature was created.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Custody administration refused or failed. No secret details are included in this error.") from None
