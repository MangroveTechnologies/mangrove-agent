"""Read refund status, verify finalized repayments, credit each original period once.

This module never signs or submits a transfer. RPC destinations use the existing
explicitly configured reconciliation allowlist, without a public fallback.
"""
from __future__ import annotations

import json
import re
import time

import httpx
from eth_utils import keccak

from src.config import app_config
from src.services import spend_service
from src.shared.db.sqlite import get_connection
from src.shared.logging import get_logger

_log = get_logger(__name__)


def watch_failed_operation(conn, operation_id, result):
    status = result.get('status', result.get('status_code', 200))
    body = result.get('body')
    failed = (isinstance(status, int) and status >= 400) or (isinstance(body, dict) and body.get('isError') is True)
    if not failed:
        return
    op = conn.execute('SELECT payment_headers FROM x402_operations WHERE id = ?', (operation_id,)).fetchone()
    if op is None or op['payment_headers'] is None:
        return
    conn.execute('''INSERT OR IGNORE INTO x402_refunds
        (reservation_id, operation_id, recovery_headers, network, amount_micro_usd, updated_at)
        SELECT id, operation_id, ?, network, amount_micro_usd, ? FROM x402_payments
        WHERE operation_id = ? AND state = 'settled' AND amount_micro_usd > 0''',
        (op['payment_headers'], spend_service._now(), operation_id))


def verify_refund(payment, status, rpc):
    from src.services.x402_inspection import TOKENS
    network = payment['network']
    token = TOKENS.get(network)
    if (token is None or str(status.get('original_tx', '')).lower() != str(payment['transaction_hash']).lower()
            or status.get('network') != network or status.get('amount_atomic') != payment['amount_micro_usd']
            or str(status.get('asset', '')).lower() != token.lower()
            or str(status.get('payer', '')).lower() != payment['wallet_address'].lower()
            or str(status.get('receiving_wallet', '')).lower() != str(payment['payee']).lower()):
        raise ValueError('Refund terms do not match the original charge')
    tx = status.get('refund_tx', '')
    if not isinstance(tx, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', tx):
        raise ValueError('Missing refund transaction')
    if tx.lower() == str(payment['transaction_hash']).lower():
        raise ValueError('Original charge is not a refund')
    if int(rpc('eth_chainId', []), 16) != int(network.split(':')[1]):
        raise ValueError('Wrong refund chain')
    receipt = rpc('eth_getTransactionReceipt', [tx])
    if not receipt:
        return None
    final = rpc('eth_getBlockByNumber', ['finalized', False])
    block = int(receipt['blockNumber'], 16)
    if block > int(final['number'], 16):
        return None
    canonical = rpc('eth_getBlockByNumber', [hex(block), False])
    if not canonical or canonical['hash'].lower() != receipt['blockHash'].lower():
        return None
    if int(receipt['status'], 16) != 1 or str(receipt['transactionHash']).lower() != tx.lower():
        raise ValueError('Refund receipt mismatch or reverted')
    topics = ['0x' + keccak(text='Transfer(address,address,uint256)').hex(),
              '0x' + payment['payee'][2:].lower().rjust(64, '0'),
              '0x' + payment['wallet_address'][2:].lower().rjust(64, '0')]
    matches = [log for log in receipt.get('logs', []) if log.get('removed', False) is False
               and str(log.get('address', '')).lower() == token.lower()
               and [str(t).lower() for t in log.get('topics', [])] == topics
               and int(log.get('data', '0x0'), 16) == payment['amount_micro_usd']]
    if len(matches) != 1:
        raise ValueError('Exact refund transfer not found')
    return {'transaction': tx.lower(), 'network': network, 'block': block,
            'block_hash': receipt['blockHash'], 'amount_atomic': payment['amount_micro_usd']}


def apply_confirmed_refund(reservation_id, status, rpc):
    raw = get_connection().execute('SELECT * FROM x402_payments WHERE id = ?', (reservation_id,)).fetchone()
    if raw is None or raw['state'] != 'settled' or status.get('state') != 'confirmed':
        return False
    payment = dict(raw)
    evidence = verify_refund(payment, status, rpc)
    if evidence is None:
        return False
    with spend_service._budget_transaction() as conn:
        current = conn.execute('SELECT * FROM x402_payments WHERE id = ?', (reservation_id,)).fetchone()
        if current is None or dict(current) != payment:
            return False
        changed = conn.execute('''UPDATE x402_refunds SET state = 'confirmed', refund_tx = ?,
            evidence_json = ?, recovery_headers = NULL, updated_at = ?
            WHERE reservation_id = ? AND state != 'confirmed' ''',
            (evidence['transaction'], json.dumps(evidence), spend_service._now(), reservation_id))
        if changed.rowcount != 1:
            return False
        budget = spend_service._get_state(conn)
        if (payment['period_id'] == budget['period_id'] and budget['exhausted']
                and spend_service._spent_micro_usd(budget['period_id'], conn) < spend_service._cap_micro_usd(budget)):
            spend_service._update_state(conn=conn, exhausted=0, exhausted_at=None, exhausted_reason=None)
    return True


def status_url(resource):
    # Never disclose the encrypted recovery proof to a new host from status data.
    configured = getattr(app_config, 'X402_MANGROVE_BASE_URL', None)
    if not configured:
        raise ValueError('Explicit Mangrove x402 origin required for refund status')
    expected, original = httpx.URL(configured), httpx.URL(resource)
    def origin(url):
        return url.scheme, url.host, url.port
    if (origin(expected) != origin(original) or expected.userinfo or original.userinfo
            or (expected.scheme != 'https' and not (expected.scheme == 'http' and expected.host in {'localhost', '127.0.0.1', '::1'}))):
        raise ValueError('Refund status origin mismatch')
    return str(expected.copy_with(path='/api/v1/x402/refund-status', query=None, fragment=None))


def run_once():
    from src.services.payment_operations import unseal
    from src.services.payment_reconciliation_worker import ReadOnlyRPC, configured_urls
    urls = configured_urls()
    if not urls:
        return
    rows = get_connection().execute('''SELECT r.*, p.resource FROM x402_refunds r
        JOIN x402_payments p ON p.id = r.reservation_id
        WHERE r.state != 'confirmed' AND r.next_attempt_at <= ?
        ORDER BY r.next_attempt_at LIMIT 5''', (time.time(),)).fetchall()
    for row in rows:
        if row['network'] not in urls:
            continue
        rpc = None
        claimed = False
        try:
            with spend_service._budget_transaction() as conn:
                claim = conn.execute('UPDATE x402_refunds SET next_attempt_at = ?, attempts = attempts + 1 '
                                     'WHERE reservation_id = ? AND state != ? AND next_attempt_at <= ?',
                                     (time.time() + 300, row['reservation_id'], 'confirmed', time.time()))
                if claim.rowcount != 1:
                    continue
                claimed = True
            saved = unseal(row['recovery_headers'])
            headers = {k: v for k, v in saved.get('headers', {}).items() if k.lower() in {
                'payment-signature', 'x-payment-recovery-token', 'x-payment-operation-id'}}
            with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                with client.stream('GET', status_url(row['resource']), headers=headers) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 16_384:
                            raise ValueError('Refund status response too large')
            status = json.loads(body)
            if status.get('state') == 'confirmed':
                rpc = ReadOnlyRPC(urls[row['network']])
                apply_confirmed_refund(row['reservation_id'], status, rpc)
            elif status.get('state') == 'review':
                with spend_service._budget_transaction() as conn:
                    conn.execute("UPDATE x402_refunds SET state = 'review' WHERE reservation_id = ? AND state != 'confirmed'",
                                 (row['reservation_id'],))
        except Exception as exc:
            # No exception text or proof goes to logs; retain the charge and retry
            # later. Unavailable status/RPC is never evidence of a refund.
            _log.warning('x402.refund.verification_pending', reservation_id=row['reservation_id'],
                         error_type=type(exc).__name__)
        finally:
            if rpc:
                rpc.close()
            if not claimed:
                continue
            with spend_service._budget_transaction() as conn:
                conn.execute("UPDATE x402_refunds SET next_attempt_at = ?, updated_at = ? WHERE reservation_id = ? AND state != 'confirmed'",
                             (time.time() + min(3600, 30 * 2**min(row['attempts'], 7)),
                              spend_service._now(), row['reservation_id']))
