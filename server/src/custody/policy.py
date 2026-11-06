"""Independent, administrator-owned spending policy and durable signature journal."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value, *, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Policy requires a nonnegative integer")
    return value


class Policy:
    """The agent cannot raise these bounds or reset this ledger via RPC."""

    def __init__(self, settings: dict, state_dir: Path):
        self.settings = settings
        self.db = sqlite3.connect(state_dir / "policy.db")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS usage (scope TEXT PRIMARY KEY, amount TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signatures (
                replay_key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                policy_digest TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS approvals (digest TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at INTEGER NOT NULL, operation TEXT NOT NULL,
                digest TEXT NOT NULL, outcome TEXT NOT NULL, policy_digest TEXT NOT NULL);
        """)
        self.db.commit()

    def _record(self, operation, request_digest, outcome):
        self.db.execute("INSERT INTO audit(at,operation,digest,outcome,policy_digest) VALUES(?,?,?,?,?)",
                        (int(time.time()), operation, request_digest, outcome, digest(self.settings)))

    def _claim(self, scope: str, amount: int, cap: int):
        row = self.db.execute("SELECT amount FROM usage WHERE scope=?", (scope,)).fetchone()
        total = int(row[0]) if row else 0
        if total + amount > cap:
            raise ValueError("Custody policy budget exhausted")
        self.db.execute("INSERT INTO usage VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET amount=excluded.amount",
                        (scope, str(total + amount)))

    def _swap(self, args):
        from src.services.transaction_intent import intent_from_dict, validate_transaction

        intent = intent_from_dict(args["intent"])
        tx = validate_transaction(args["unsigned_tx"], intent, args["wallet_address"], args.get("chain_id"))
        requested = args["intent"]
        kind = requested["kind"]
        token = requested["token_address"] if kind == "approval" else requested["input_token_address"]
        router = requested["spender"] if kind == "approval" else requested["router_address"]
        rule = next((r for r in self.settings.get("swap_rules", [])
                     if r.get("wallet_address", "").lower() == args["wallet_address"].lower()
                     and r["chain_id"] == requested["chain_id"]
                     and r["input_token"].lower() == token.lower()
                     and r["router"].lower() == router.lower()
                     and (kind == "approval" or r["output_token"].lower() == requested["output_token_address"].lower())), None)
        if rule is None:
            raise ValueError("No independently authorized swap rule")
        amount = integer(requested["amount"], minimum=1)
        if amount > integer(rule["max_amount"], minimum=1):
            raise ValueError("Swap exceeds the independently authorized amount")
        if kind == "swap":
            from src.services.transaction_intent import swap_executor

            if integer(requested.get("routing_fee_bps", 0)) > integer(rule.get("max_routing_fee_bps", 0)):
                raise ValueError("Swap routing fee exceeds independently authorized policy")
            if swap_executor(tx) not in {executor.lower() for executor in rule.get("allowed_executors", [])}:
                raise ValueError("Swap executor is not independently authorized")
            numerator = integer(rule["min_rate_numerator"], minimum=1)
            denominator = integer(rule["min_rate_denominator"], minimum=1)
            if integer(requested["min_return_amount"], minimum=1) * denominator < amount * numerator:
                raise ValueError("Swap violates the independently authorized exchange-rate floor")
        gas, fee = integer(tx["gas"], minimum=1), integer(tx.get("maxFeePerGas", tx.get("gasPrice")), minimum=1)
        if (gas > integer(rule["max_gas"], minimum=1)
                or fee > integer(rule["max_fee_per_gas"], minimum=1)
                or gas * fee > integer(rule["max_total_fee_wei"], minimum=1)
                or integer(tx.get("value", 0)) > integer(rule.get("max_native_value", 0))):
            raise ValueError("Swap violates custody gas or native-value policy")
        scope = f"{kind}:{args['wallet_address'].lower()}:{requested['chain_id']}:{token.lower()}:{router.lower()}"
        self._claim(scope, amount, integer(rule["lifetime_amount"], minimum=1))
        self._claim(f"gas:{args['wallet_address'].lower()}:{requested['chain_id']}",
                    gas * fee, integer(rule["lifetime_total_fee_wei"], minimum=1))
        return f"tx:{args['wallet_address'].lower()}:{tx['chainId']}:{tx['nonce']}"

    def _x402(self, args, *, claim=True):
        from src.services.wallet_manager import _validate_x402_authorization

        payee, value, chain_id = _validate_x402_authorization(
            args["domain"], args["types"], args["primary_type"], args["message"], args["wallet_address"])
        rule = self.settings.get("x402", {})
        message = args["message"]
        now = int(time.time())
        if (chain_id != rule.get("chain_id")
                or rule.get("wallet_address", "").lower() != args["wallet_address"].lower()
                or payee.lower() not in {p.lower() for p in rule.get("payees", [])}
                or value <= 0 or value > integer(rule.get("max_payment_micro_usd", 0))
                or message["validAfter"] > now or message["validBefore"] <= now
                or message["validBefore"] - now > integer(rule.get("max_validity_seconds", 0))):
            raise ValueError("Payment violates independent custody policy")
        if claim:
            self._claim("x402", value, integer(rule.get("lifetime_micro_usd", 0)))
        nonce = message["nonce"]
        if isinstance(nonce, str):
            nonce = nonce.removeprefix("0x").lower()
        else:
            nonce = bytes(nonce).hex()
        return f"x402:{args['wallet_address'].lower()}:{chain_id}:{nonce}"

    def authorize(self, operation, args):
        if operation == "sign":
            return self._swap(args)
        if operation == "sign_x402_authorization":
            return self._x402(args)
        if operation == "sign_siwx_message":
            from urllib.parse import urlsplit

            from src.services.wallet_manager import _validate_siwx

            _validate_siwx(**args)
            target = urlsplit(args["uri"])
            origin = f"{target.scheme}://{target.netloc}"
            if (origin not in self.settings.get("siwx_origins", [])
                    or args["chain"] not in self.settings.get("identity_chains", [])
                    or args["wallet_address"].lower() not in {wallet.lower() for wallet in self.settings.get("identity_wallets", [])}):
                raise ValueError("Identity destination is not authorized by custody policy")
            return "siwx:" + digest(args)
        if operation == "sign_marketplace_proof":
            from src.services.wallet_manager import _validate_marketplace_proof

            _validate_marketplace_proof(**args)
            request_digest = digest({"operation": operation, "arguments": args})
            approved = self.db.execute("SELECT created_at FROM approvals WHERE digest=?", (request_digest,)).fetchone()
            if not approved or approved[0] < int(time.time()) - 600:
                raise ValueError("Marketplace proof requires an exact, recent human approval")
            self.db.execute("DELETE FROM approvals WHERE digest=?", (request_digest,))
            return "marketplace:" + request_digest
        raise ValueError("Unknown signing operation")

    def sign(self, operation: str, args: dict, sign_function):
        request_digest = digest({"operation": operation, "arguments": args})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Exact retries return the protected stored signature and never reserve twice.
            previous = self.db.execute("SELECT result FROM signatures WHERE digest=? AND policy_digest=? AND result IS NOT NULL",
                                       (request_digest, digest(self.settings))).fetchone()
            if previous:
                # A cached signature can still be spendable. Recheck time-bound
                # proof/payment validity without claiming another budget unit or
                # consuming the already used human marketplace approval.
                if operation == "sign_x402_authorization":
                    self._x402(args, claim=False)
                elif operation == "sign_siwx_message":
                    self.authorize(operation, args)
                elif operation == "sign_marketplace_proof":
                    from src.services.wallet_manager import _validate_marketplace_proof
                    _validate_marketplace_proof(**args)
                self._record(operation, request_digest, "replayed")
                self.db.commit()
                return json.loads(previous[0])
            replay_key = self.authorize(operation, args)
            previous = self.db.execute("SELECT digest FROM signatures WHERE replay_key=?", (replay_key,)).fetchone()
            if previous:
                raise ValueError("A different or uncertain request already used this signing nonce")
            self.db.execute("INSERT INTO signatures(replay_key,digest,policy_digest) VALUES(?,?,?)",
                            (replay_key, request_digest, digest(self.settings)))
            self._record(operation, request_digest, "reserved")
            # Commit BEFORE signing: a crash/exception after signing must not release budget.
            self.db.commit()
        except Exception:
            self.db.rollback()
            self._record(operation, request_digest, "refused")
            self.db.commit()
            raise
        try:
            result = sign_function()
            self.db.execute("UPDATE signatures SET result=? WHERE replay_key=?", (canonical(result), replay_key))
            self._record(operation, request_digest, "signed")
            self.db.commit()
            return result
        except Exception:
            self._record(operation, request_digest, "failed_reserved")
            self.db.commit()
            raise
