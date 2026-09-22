"""Persist an async SDK's payment proof before it leaves the process."""
import secrets

import httpx

from src.services import payment_operations


class OperationAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport
        self.capability = None

    async def handle_async_request(self, request):
        oid = payment_operations.current_operation.get()
        if oid:
            request.headers['X-Payment-Operation-Id'] = oid
            payment = {k: v for k, v in request.headers.items() if k.lower() in {'payment-signature', 'x-payment'}}
            if payment:
                request.headers['X-Payment-Recovery-Token'] = secrets.token_urlsafe(32)
                payment['X-Payment-Recovery-Token'] = request.headers['X-Payment-Recovery-Token']
                payment_operations.save_headers(oid, {'headers': payment, 'idempotency': self.capability})
        response = await self.transport.handle_async_request(request)
        if response.status_code == 402:
            self.capability = response.headers.get('X-Payment-Idempotency')
        return response

    async def aclose(self):
        await self.transport.aclose()
