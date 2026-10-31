"""Local human OAuth setup. Credentials never enter MCP tool arguments."""
import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.services import mangrove_oauth as oauth
from src.shared.auth.dependency import require_api_key


def require_local_auth(key=Depends(require_api_key)):
    if not key or oauth.app_config.AUTH_ENABLED is not True:
        raise oauth.OAuthError('Local authentication must be enabled for OAuth setup.')
    return key


router = APIRouter(prefix='/oauth', tags=['connection'])


class _CallbackLogFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access tuple contains the full query including code and state.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if str(args[2]).split('?', 1)[0].rstrip('/') == '/api/v1/agent/oauth/callback':
                args[2] = '/api/v1/agent/oauth/callback'
                record.args = tuple(args)
        return True


logging.getLogger('uvicorn.access').addFilter(_CallbackLogFilter())


class StartRequest(BaseModel):
    scopes: str = Field(default=oauth.SCOPES, max_length=512)


@router.post('/start', dependencies=[Depends(require_local_auth)])
def start(body: StartRequest):
    return oauth.start(body.scopes)


@router.get('/status', dependencies=[Depends(require_local_auth)])
def status():
    return oauth.status()


@router.post('/cancel', dependencies=[Depends(require_local_auth)])
def cancel():
    return oauth.cancel()


@router.post('/disconnect', dependencies=[Depends(require_local_auth)])
def disconnect():
    return oauth.disconnect()


@router.get('/callback', include_in_schema=False)
def callback(request: Request):
    # Browser cannot carry the agent API key. Single-use state, issuer and PKCE
    # bind this request to the authenticated local /start operation instead.
    try:
        if any(len(request.query_params.getlist(k)) != 1 for k in request.query_params):
            raise oauth.OAuthError('Duplicate callback parameters.')
        oauth.complete(dict(request.query_params))
        nonce = secrets.token_urlsafe(18)
        content = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Mangrove connection</title></head>
<body>
  <main>
    <h1>Mangrove is connected</h1>
    <p>Your account is ready. Return to setup to finish.</p>
    <p>This tab will close in a moment. If it stays open, close it and return to setup.</p>
  </main>
  <script nonce="{nonce}">window.setTimeout(function () {{ window.close(); }}, 1500);</script>
</body>
</html>"""
        headers = {
            'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
        }
        return HTMLResponse(content, status_code=200, headers=headers)
    except oauth.OAuthError:
        content = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                   '<title>Mangrove connection</title></head><body><main><h1>Sign-in did not complete</h1>'
                   '<p>Return to setup and start sign-in again.</p></main></body></html>')
        return HTMLResponse(content, status_code=400, headers={
            'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
        })
