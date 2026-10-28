# Connect with Mangrove sign-in or an API key

Run `./scripts/setup.sh` and choose **Sign in to Mangrove in your browser (OAuth)**
or **Use an API key**. Wallet payment remains a separate explicit option.

For browser sign-in:

```bash
./scripts/setup.sh --auth oauth
```

Setup starts the local agent, opens the configured Mangrove consent page, waits for sign-in and
permission approval, then continues connection verification. If the browser does
not open, use the printed authorization URL on the same computer. The callback
returns to the configured loopback agent URL; Docker publishes this on localhost.
Remote/headless installs require a browser that can reach that machine's loopback
callback (for example an explicitly configured local port tunnel). Setup does not
paste credentials into chat, create wallets or make payments.

The backend must have the OAuth implementation deployed and enabled, with a
working developer-portal consent page. Deploy `src/MangroveAdmin/frontend` from
MangroveAI and set `MCP_OAUTH_CONSENT_URL` to the developer portal's
`/oauth/consent` URL (production configuration:
`https://app.mangrovedeveloper.ai/oauth/consent`). The browser uses the portal's
existing Firebase sign-in and calls MangroveAI's authenticated
`/api/v1/auth/oauth/authorizations/{requestID}` consent API. The platform frontend
and its consent bridge are not the browser entry point for this flow. The agent
never receives the browser session or a gateway service credential.

Platform-backed accounts still require the gateway's live authority endpoint,
`/internal/v1/agent/oauth-authority`, with matching `PLATFORM_API_URL` and
`PLATFORM_AGENT_SERVICE_KEY` configuration on MangroveAI. Deploy that authority
endpoint before enabling the AI integration. This verifies current account and
organization access; it does not expose platform business tools to the agent.
Migrations 116 and 117 run through the existing deployment migration lifecycle.
Migration 117 revokes legacy OAuth grants once, requiring users to reconnect;
rollback must not reactivate those grants. Do not run migrations per instance.
While OAuth is disabled its sign-in endpoints return 404: the agent fails
explicitly and does not switch authentication modes.
The default issuer is `https://api.mangrovedeveloper.ai`. An intentional development
server can be configured with `MANGROVE_OAUTH_ISSUER` (HTTPS or loopback HTTP only).
API-key destination overrides do not change the OAuth issuer or token audience.

Existing API-key setup remains available:

```bash
./scripts/setup.sh --auth api-key
```

The key prompt hides input. Switching modes preserves the saved key but OAuth
never uses it, including after a rejected/expired grant. Restart through setup
when changing modes. A separately saved OAuth grant can be disconnected before
switching; changing modes does not automatically revoke the other credential.

## Reconnect, additional permissions and disconnect

Use the same setup entry point for sign-in and extra permissions. Setup automatically uses a saved OAuth connection and reports that you are already signed in. If sign-in is needed, it opens the browser automatically. `--oauth login` explicitly starts a new browser sign-in when you want to reconnect or change accounts. Status and disconnect require the local agent to be running:

```bash
./scripts/setup.sh --oauth status
./scripts/setup.sh --auth oauth --oauth login
./scripts/setup.sh --auth oauth --oauth login --oauth-scope signals:evaluate
./scripts/setup.sh --auth oauth --oauth login --oauth-scope backtests:run
./scripts/setup.sh --oauth disconnect
```

Additional permissions require a new human approval in the portal. Cancelling
leaves the previous connection intact. Successful reconnect saves the new grant before
revoking the previous one. If revocation is unavailable, its encrypted credential
is retained for disconnect; another reconnect is blocked until cleanup. Requests are not automatically replayed
after an authorization error. Disconnect immediately disables local use; if remote
revocation cannot be confirmed, rerun disconnect when the original issuer is
available and follow its cleanup instructions. You may also revoke the connected
app in the portal, but that does not replace local disconnect cleanup. The local
command confirms when all retained grants have been cleaned up.

## Available operations

OAuth accesses the authoritative `/mcp/connect` catalog. In backend version one,
strategy saving/updating/archiving and sweep tools are excluded. Those capabilities
require an appropriately authorized API-key or x402 connection. External agents
use MangroveAI strategy definitions in every mode; platform inventory, marketplace,
notifications and platform deployment proposals remain internal to Michael in the
platform. OAuth permission approval does not expose those tools. Local features that depend on the REST SDK, including paper
scheduling, and authenticated Markets features are not OAuth-capable. OAuth discovery
omits these adapters, including swap execution, Markets ownership preparation/submission,
and Kraken fill telemetry. Calls from a stale catalog return `OAUTH_TOOL_UNAVAILABLE`
before any operation or payment. The agent never sends the token to another service
or falls back to a saved key or wallet. Local wallet custody and direct broker tools
remain available with their existing local authentication and approval requirements;
they do not become remotely authorized by OAuth.

## Credential lifecycle and verification

Tokens are encrypted with the existing local master key in `oauth.db` beside the
agent database; the file is permission-restricted. Preserve the master key when
backing up this file. If you decline “Stay connected without signing in again,” the connection works
until its access token expires, then requires browser sign-in again. It never
attempts a refresh. Disconnect and replacement revoke either type of connection.
With that permission approved, tokens are refreshed before expiry under a process
lock and SQLite transaction. A durable refresh-pending marker is saved before sending the
request: a timeout or process crash requires reconnecting rather than replaying a
possibly rotated refresh token. The local agent remains one worker per installation.
Callback state and PKCE verifiers expire after ten minutes and are single-use.
A restart cancels pending browser sign-in. Callback query secrets are removed from
Uvicorn access records, and status endpoints never return credentials.

Release acceptance still requires a real browser round trip against the deployed
OAuth backend: login, consent cancellation, additional consent, expiry/refresh,
revocation and reconnect; verify both bare and Docker callback paths and the
installed client artifact. Offline tests do not establish portal or production
availability. OAuth remains disabled by default on the backend until released.

### Changing the OAuth issuer

Disconnect while the agent is still configured for the old issuer:

```bash
./scripts/setup.sh --oauth disconnect
```

Wait for `Remote grant revoked.` before changing `MANGROVE_OAUTH_ISSUER` and
restarting setup for the new issuer. A failed or uncertain revocation keeps an
encrypted cleanup receipt and blocks replacement sign-in; retry disconnect when
the original issuer is available. Revoking in the portal alone does not remove
that local receipt.

If configuration was already changed, the error identifies the remaining issuer.
Restore that issuer in `MANGROVE_OAUTH_ISSUER`, restart the agent, and run disconnect
again. Older installations may have receipts for two issuers: explicitly select
each indicated issuer and disconnect in turn, then select the intended issuer and
sign in. Each successful revocation removes only that grant's receipt. Tokens are
never sent to an issuer selected only from stored metadata, and no database or
credential-file deletion is needed.
