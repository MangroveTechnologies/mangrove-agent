"""Bounded, restart-safe reconciliation. Never signs, retries a purchase or pays.

Operators configure RPC destinations explicitly. There is no public-RPC fallback.
The worker uses finalized chain evidence and keeps all ambiguous amounts reserved.
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone

import httpx
from eth_utils import keccak

from src.config import app_config
from src.services import spend_service
from src.services.x402_inspection import TOKENS, inspect_authorization
from src.services.x402_reconciliation import reconcile_authorization
from src.shared.logging import get_logger

_log = get_logger(__name__)
_READ_METHODS = {'eth_chainId', 'eth_getBlockByNumber', 'eth_call', 'eth_getLogs', 'eth_getTransactionReceipt'}


def configured_urls() -> dict[str, str]:
    raw = getattr(app_config, 'X402_RECONCILIATION_RPC_URLS', None) or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError('Reconciliation RPC configuration must map network IDs to URLs')
    for network, value in raw.items():
        url = httpx.URL(value)
        if (network not in TOKENS or not url.host or url.userinfo or url.fragment
                or (url.scheme != 'https' and not (url.scheme == 'http' and url.host in {'127.0.0.1', 'localhost', '::1'}))):
            raise ValueError('Invalid reconciliation RPC destination')
    return raw


class ReadOnlyRPC:
    def __init__(self, url):
        self.url = url
        self.client = httpx.Client(timeout=5, trust_env=False, follow_redirects=False)

    def __call__(self, method, params):
        if method not in _READ_METHODS:
            raise ValueError('Reconciliation only supports read methods')
        with self.client.stream('POST', self.url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise ValueError('RPC response exceeds reconciliation limit')
            data = json.loads(body)
        if not isinstance(data, dict) or 'error' in data or 'result' not in data or data.get('id') != 1:
            raise ValueError('RPC evidence unavailable')
        return data['result']

    def close(self):
        self.client.close()


def _find_transaction(row, evidence, rpc):
    """Scan a bounded finalized window per run; exact receipt checks follow."""
    end = min(row['scan_block'] if row['scan_block'] is not None else evidence['finalized_block'], evidence['finalized_block'])
    start = max(0, end - 1999)
    topics = ['0x' + keccak(text=name).hex() for name in ('AuthorizationUsed(address,bytes32)', 'AuthorizationCanceled(address,bytes32)')]
    logs = rpc('eth_getLogs', [{'address': row['asset'], 'fromBlock': hex(start), 'toBlock': hex(end),
                             'topics': [topics, '0x' + row['wallet_address'][2:].lower().rjust(64, '0'), row['authorization_nonce']]}])
    if not isinstance(logs, list) or len(logs) > 100:
        raise ValueError('Malformed or excessive authorization logs')
    transactions = {log.get('transactionHash') for log in logs if isinstance(log, dict) and log.get('removed', False) is False}
    if len(transactions) == 1:
        transaction = transactions.pop()
        if isinstance(transaction, str) and re.fullmatch(r'0x[0-9a-fA-F]{64}', transaction):
            return transaction, start - 1
    if transactions:
        raise ValueError('Ambiguous transaction evidence')
    created = datetime.fromisoformat(row['created_at'])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    block = rpc('eth_getBlockByNumber', [hex(start), False])
    if start == 0 or int(block['timestamp'], 16) < created.timestamp() - 60:
        return None, None  # next backoff may scan again; never infer unpaid
    return None, start - 1


def run_once(*, urls=None, rpc_factory=ReadOnlyRPC, now=None, batch_size=5):
    destinations = configured_urls() if urls is None else urls
    if not destinations:
        return 0
    now = time.time() if now is None else now
    # Claim work durably before any network I/O. Crashed workers become eligible
    # again after a bounded lease. Concurrent inspections remain CAS-protected.
    with spend_service._budget_transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO x402_reconciliation_jobs(reservation_id) SELECT id FROM x402_payments WHERE state = 'authorized'")
        rows = conn.execute("SELECT p.*, j.attempts, j.scan_block FROM x402_payments p JOIN x402_reconciliation_jobs j ON j.reservation_id = p.id "
                            "WHERE p.state = 'authorized' AND j.next_check <= ? ORDER BY j.next_check, p.created_at LIMIT ?", (now, batch_size)).fetchall()
        for row in rows:
            conn.execute('UPDATE x402_reconciliation_jobs SET next_check = ? WHERE reservation_id = ?', (now + 300, row['id']))
    for raw in rows:
        row = dict(raw)
        outcome, scan_block, rpc = 'rpc_not_configured', row['scan_block'], None
        try:
            if row['network'] in destinations:
                rpc = rpc_factory(destinations[row['network']])
                evidence = inspect_authorization(row, rpc)
                outcome = evidence['outcome']
                transaction = row['transaction_hash']
                if outcome == 'used_or_cancelled' and not transaction:
                    transaction, scan_block = _find_transaction(row, evidence, rpc)
                if transaction or outcome == 'expired_unused_at_finalized_block':
                    outcome = reconcile_authorization(row['id'], rpc, transaction=transaction)['outcome']
        except Exception as error:
            outcome = 'inspection_unavailable'
            _log.warning('x402.reconciliation.unavailable', reservation_id=row['id'], error_type=type(error).__name__)
        finally:
            if rpc is not None and hasattr(rpc, 'close'):
                rpc.close()
        delay = min(3600, 15 * 2 ** min(row['attempts'], 8)) + random.uniform(0, 5)
        with spend_service._budget_transaction() as conn:
            conn.execute('UPDATE x402_reconciliation_jobs SET attempts = attempts + 1, next_check = ?, outcome = ?, scan_block = ? WHERE reservation_id = ?',
                         (now + delay, outcome, scan_block, row['id']))
        _log.info('x402.reconciliation.checked', reservation_id=row['id'], outcome=outcome)
    return len(rows)
