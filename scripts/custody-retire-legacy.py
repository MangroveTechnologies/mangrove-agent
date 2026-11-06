"""Root-only finalization after a verified custody migration of a stopped agent."""
import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from src.custody.transport import check_no_extended_acl


def retire(database: Path, receipt: Path, custody_uid: int):
    if os.geteuid() != 0 or custody_uid <= 0:
        raise ValueError("Only the independent host administrator can retire legacy custody")
    for path in (receipt, database):
        for parent in path.parents:
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid not in {0, custody_uid} or info.st_mode & 0o022:
                raise ValueError("Migration receipt and stopped database require protected root/custody directory ancestors")
            check_no_extended_acl(parent)
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, custody_uid} or info.st_mode & 0o077:
        raise ValueError("Stage the stopped legacy database as a private root/custody-owned regular file before retirement")
    check_no_extended_acl(database)
    check_no_extended_acl(receipt)
    descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != custody_uid or info.st_mode & 0o077:
            raise ValueError("Migration receipt must belong exclusively to custody")
        evidence = json.load(stream)
    expected = {row["address"]: row["ciphertext_sha256"] for row in evidence["wallets"]}
    connection = sqlite3.connect(database, timeout=1)
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("BEGIN EXCLUSIVE")
    try:
        rows = connection.execute("SELECT address,encrypted_secret,encryption_method FROM wallets").fetchall()
        for address, ciphertext, method in rows:
            if method == "external-custody":
                continue
            if address not in expected or hashlib.sha256(bytes(ciphertext)).hexdigest() != expected[address]:
                raise ValueError("Legacy database changed or was not completely migrated")
        connection.execute("UPDATE wallets SET encrypted_secret=X'', encryption_method='external-custody'")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-db", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--custody-uid", type=int, required=True)
    args = parser.parse_args()
    retire(args.legacy_db, args.receipt, args.custody_uid)
    print("Active legacy wallet ciphertext retired. Recovery copies, filesystem snapshots and retained master-key backups require separate administrator review; deletion cannot prove historical copies erased.")
