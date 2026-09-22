"""Durable identity for an outbound paid operation, separate from its payment.

Only encrypted payment headers/results are persisted. No URL, body, signature or
private key appears in diagnostics. Pending fingerprints deduplicate SDK retries
which lack a caller key; explicit identities survive successful completion too.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from contextvars import ContextVar

from src.services import spend_service
from src.shared.crypto.fernet import decrypt, encrypt, require_existing_master_key
from src.shared.errors import ValidationError, X402PaymentUncertain

current_operation: ContextVar[str | None] = ContextVar('x402_operation', default=None)
MAX_RESULT_BYTES = 8 * 1024 * 1024


def fingerprint(payer: str, network: str, method: str, resource: str, content: bytes) -> str:
    digest = hashlib.sha256()
    for part in (payer.lower().encode(), network.encode(), method.upper().encode(), resource.encode(), content):
        digest.update(len(part).to_bytes(8, 'big'))
        digest.update(part)
    return digest.hexdigest()


def begin(digest: str, operation_id: str | None = None) -> tuple[dict, bool]:
    if operation_id is not None:
        try:
            operation_id = str(uuid.UUID(operation_id))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError('Payment operation identity must be a UUID.') from None
    with spend_service._budget_transaction() as conn:
        row = conn.execute('SELECT * FROM x402_operations WHERE id = ?', (operation_id,)).fetchone() if operation_id else None
        if row is not None:
            if row['fingerprint'] != digest:
                raise ValidationError('Payment operation identity was reused with different request parameters.')
            if row['state'] == 'unsigned_failed':
                previous = conn.execute('SELECT 1 FROM x402_payments WHERE operation_id = ? LIMIT 1', (row['id'],)).fetchone()
                if previous:
                    # A previously signed, reconciled attempt is closed. New
                    # attempts require a new identity at the receiver too.
                    raise ValidationError('This payment attempt is closed; use a new operation identity.')
                pending = conn.execute("SELECT * FROM x402_operations WHERE fingerprint = ? AND state = 'pending'", (digest,)).fetchone()
                if pending is not None:
                    return dict(pending), False
                # A failure before any reservation is safe to reclaim.
                conn.execute("UPDATE x402_operations SET state = 'pending', updated_at = ? WHERE id = ?",
                             (spend_service._now(), row['id']))
                return {**dict(row), 'state': 'pending'}, True
            return dict(row), False
        # Without a caller identity, identical pending work is recovery. With
        # one, a different ID still cannot replace this outstanding payment.
        row = conn.execute("SELECT * FROM x402_operations WHERE fingerprint = ? AND state = 'pending'", (digest,)).fetchone()
        if row is not None:
            return dict(row), False
        oid, now = operation_id or str(uuid.uuid4()), spend_service._now()
        conn.execute("INSERT INTO x402_operations(id, fingerprint, state, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
                     (oid, digest, now, now))
        return {'id': oid, 'state': 'pending', 'payment_headers': None, 'response': None}, True


def require_unsigned(conn, operation_id: str) -> None:
    row = conn.execute('SELECT state FROM x402_operations WHERE id = ?', (operation_id,)).fetchone()
    ids = [r[0] for r in conn.execute("SELECT id FROM x402_payments WHERE operation_id = ? AND state != 'released'", (operation_id,))]
    if row is None or row['state'] != 'pending' or ids:
        raise X402PaymentUncertain(operation_id=operation_id, reservation_ids=ids)


def reservation_ids(operation_id: str) -> list[str]:
    with spend_service._budget_transaction() as conn:
        return [r[0] for r in conn.execute('SELECT id FROM x402_payments WHERE operation_id = ?', (operation_id,))]


def seal(value: dict) -> bytes:
    require_existing_master_key()
    return encrypt(json.dumps(value, separators=(',', ':')).encode())


def unseal(value: bytes) -> dict:
    return json.loads(decrypt(value))


def save_headers(operation_id: str, headers: dict) -> None:
    encrypted = seal(headers)
    with spend_service._budget_transaction() as conn:
        cursor = conn.execute("UPDATE x402_operations SET payment_headers = ?, updated_at = ? WHERE id = ? AND state = 'pending' AND payment_headers IS NULL",
                              (encrypted, spend_service._now(), operation_id))
        if cursor.rowcount != 1:
            raise X402PaymentUncertain(operation_id=operation_id)


def complete(operation_id: str, result: dict) -> None:
    encrypted = seal(result)
    with spend_service._budget_transaction() as conn:
        from src.services.refund_service import watch_failed_operation
        watch_failed_operation(conn, operation_id, result)
        conn.execute("UPDATE x402_operations SET state = 'complete', response = ?, payment_headers = NULL, updated_at = ? WHERE id = ? AND state = 'pending'",
                     (encrypted, spend_service._now(), operation_id))


def abandon_unsigned(operation_id: str) -> None:
    with spend_service._budget_transaction() as conn:
        conn.execute("UPDATE x402_operations SET state = 'unsigned_failed', updated_at = ? WHERE id = ? AND state = 'pending' "
                     "AND payment_headers IS NULL AND NOT EXISTS (SELECT 1 FROM x402_payments WHERE operation_id = ? AND state != 'released')",
                     (spend_service._now(), operation_id, operation_id))


def tracked_payment(function):
    """Operation identity for explicit async HTTP/MCP payer entrypoints."""
    import functools
    import inspect
    from dataclasses import asdict

    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        import httpx

        from src.services import x402_payer
        parameters = inspect.signature(function).bind(*args, **kwargs)
        parameters.apply_defaults()
        values = parameters.arguments
        headers = httpx.Headers(values.get('headers') or {})
        resource = values.get('url') or values.get('resource')
        if any(k.lower() in {'authorization', 'x-api-key', 'payment-signature', 'x-payment'} for k in headers) or httpx.URL(resource).userinfo:
            raise ValidationError('Payment requests cannot contain credentials or an existing signature.')
        if values.get('url'):
            url = httpx.URL(resource)
            if not url.host or not (url.scheme == 'https' or (
                url.scheme == 'http' and url.host in {'localhost', '127.0.0.1', '::1'}
            )):
                raise ValidationError('Payment requests require HTTPS or HTTP loopback.')
            timeout = values['timeout']
            if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValidationError('The payment request timeout must be finite and positive.')
            # Only the operation transport may attach its private recovery token.
            headers.pop('X-Payment-Recovery-Token', None)
            values['headers'] = dict(headers)
        payer = x402_payer.resolve_payer_wallet(values.get('wallet_address'))
        network = x402_payer.get_network()
        content = values.get('content') or b''
        if isinstance(content, str):
            content = content.encode()
        method = values.get('method') or 'MCP:' + values.get('name', '')
        digest = fingerprint(payer, network, method, resource, content)
        operation, created = begin(digest, headers.get('X-Payment-Operation-Id'))
        oid = operation['id']
        if not created:
            if operation['response']:
                cached = unseal(operation['response'])
                if 'status' in cached:
                    import base64
                    response = httpx.Response(cached['status'], headers=cached['headers'], content=base64.b64decode(cached['body']))
                    settlement = x402_payer.decode_settlement(response, payer=payer, network=network)
                    return x402_payer.PaymentResult(status_code=response.status_code, body=x402_payer._decode_body(response), paid=settlement is not None,
                                                   transaction=settlement.get('transaction') if settlement else None, network=network, payer=payer)
                return x402_payer.PaymentResult(**cached)
            if values.get('url') and operation['payment_headers']:
                recovery = unseal(operation['payment_headers'])
                if recovery.get('idempotency') == 'v1':
                    try:
                        outgoing = httpx.Headers(headers)
                        outgoing.update(recovery['headers'])
                        outgoing['X-Payment-Operation-Id'] = oid
                        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False),
                                                     timeout=values['timeout'], trust_env=False, follow_redirects=False) as client:
                            response = await client.request(method, resource, headers=outgoing, content=content)
                        ids = reservation_ids(oid)
                        settlement = x402_payer.decode_settlement(response, payer=payer, network=network)
                        if settlement is None:
                            raise X402PaymentUncertain(operation_id=oid, reservation_ids=ids)
                        spend_service.reconcile(ids, status_code=response.status_code, settlement=settlement)
                        if response.headers.get('X-Payment-Operation-State') == 'pending':
                            error = X402PaymentUncertain(operation_id=oid, reservation_ids=ids)
                            error.payment_state = 'settled'
                            raise error
                        result = x402_payer.PaymentResult(status_code=response.status_code, body=x402_payer._decode_body(response), paid=True,
                                                         transaction=settlement['transaction'], network=network, payer=payer)
                        complete(oid, asdict(result))
                        return result
                    except X402PaymentUncertain:
                        raise
                    except Exception:
                        # A recovery transport or ledger failure cannot justify
                        # another payment. Do not expose URLs or provider text.
                        raise X402PaymentUncertain(operation_id=oid) from None

            raise X402PaymentUncertain(operation_id=oid, reservation_ids=reservation_ids(oid))
        token = current_operation.set(oid)
        try:
            result = await function(*parameters.args, **parameters.kwargs)
            if isinstance(result.body, dict) and result.body.get('error') in {
                'payment_operation_pending', 'payment_result_persistence_pending', 'payment_receipt_persistence_pending',
            }:
                error = X402PaymentUncertain(operation_id=oid, reservation_ids=reservation_ids(oid))
                error.payment_state = 'settled' if result.paid else 'unresolved'
                raise error
            if reservation_ids(oid):
                encoded = json.loads(json.dumps(asdict(result), default=lambda value: value.model_dump(by_alias=True)))
                if len(json.dumps(encoded).encode()) <= MAX_RESULT_BYTES:
                    complete(oid, encoded)
            return result
        except X402PaymentUncertain as error:
            error.operation_id = oid
            raise
        finally:
            current_operation.reset(token)
            abandon_unsigned(oid)
    return wrapped


def pending_status() -> list[dict]:
    """Identifiers and payment state only; recovery secrets never leave storage."""
    with spend_service._budget_transaction() as conn:
        rows = conn.execute("SELECT o.id, o.created_at, COUNT(p.id) AS payment_count, "
                            "COALESCE(SUM(CASE WHEN p.state = 'authorized' THEN p.amount_micro_usd ELSE 0 END), 0) AS pending_micro_usd, "
                            "COALESCE(SUM(CASE WHEN p.state = 'settled' THEN p.amount_micro_usd ELSE 0 END), 0) AS settled_micro_usd "
                            "FROM x402_operations o LEFT JOIN x402_payments p ON p.operation_id = o.id "
                            "WHERE o.state = 'pending' GROUP BY o.id ORDER BY o.created_at LIMIT 100").fetchall()
    return [{**dict(row), 'retry_payment': False} for row in rows]
