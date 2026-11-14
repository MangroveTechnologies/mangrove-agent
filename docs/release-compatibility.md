# SDK and local-deployment release compatibility

The Agent installs the published Python SDKs pinned in `server/requirements.lock`:
`mangroveai` for MangroveAI and `mangrovemarkets` for MangroveMarkets. Their imports
are `mangrove_ai` and `mangrove_markets`. The Agent itself is installed from Git or a
repository plugin, not from a published `mangrove-agent` PyPI package. Every install
path (`scripts/setup.sh`, `scripts/run-bare.sh`, `scripts/plugin-start-agent.sh` and
`server/Dockerfile`) installs the lock, so the lock decides which SDK version runs.

## Current locked release

The normal and custody installs pin published `mangroveai==2.5.0`, while
`mangrovemarkets==1.1.1` is unchanged. MangroveAI SDK 2.5.0 omits unset legacy
backtest controls so the service selects its current defaults, and preserves
optional signal capability metadata. It retains the local-deployment helper and
flat legacy import shim. This dependency update does not change Mangrove's risk
policy, activate a strategy, upgrade an existing installation automatically, or
establish acceptance of live trading and provider workflows.

## Local deployment requirements

The local Robinhood deployment tools read a validated strategy snapshot through the
SDK's `client.strategies.local_deployment` helper. They need:

- MangroveAI Python SDK 2.3.0 or later, which contains the helper. The SDK floor in
  `server/requirements.txt` is at or above this version.
- MangroveAI API 7.11.0 or later, which serves
  `GET /api/v1/strategies/{id}/local-deployment`.

If the installed SDK lacks the helper, the Agent refuses the workflow with a
capability error before it makes a request. The Agent runtime uses Python, so it
needs no TypeScript SDK.

The SDK's coexistence with the private MangroveAI engine on a case-insensitive
filesystem is a packaging check for the MangroveAI API image, which installs both.
The Agent does not install the private engine, so that check does not apply here.

## Check the installed SDK

Check the installed package, not an SDK source checkout:

```sh
.venv/bin/python -c 'from mangrove_ai._services.strategies import StrategiesService; assert callable(getattr(StrategiesService, "local_deployment", None)), "Installed SDK lacks local_deployment support"'
```

CI runs `server/tests/integration/test_installed_sdk_local_deployment.py` with
`MANGROVE_LOCAL_DEPLOYMENT_EXPECTED=present`. The test fails if the locked SDK lacks
the helper, and it sends the installed SDK's real HTTP request to a loopback server
that returns the synthetic fixture `server/tests/fixtures/local-deployment-not-ready.json`.

## Upgrade the SDK

1. Change the `mangroveai` range in `server/requirements.txt`.
2. Regenerate the lock with the command at the top of `server/requirements.txt`, and
   confirm the diff changes only the packages you intended to change. For a
   bounded SDK update, use `--upgrade-package mangroveai` to preserve other pins.
   Regenerate `server/requirements-custody.lock` with
   `python scripts/hash_custody_lock.py` and verify its PyPI wheel hashes.
3. Run the test suite from `server/` with `ENVIRONMENT=test`.
4. Verify an authenticated deployment against a real strategy only with the user's
   separate authorization. Installing an SDK and passing CI do not authorize
   activation, broker orders, payments or paid provider calls.
