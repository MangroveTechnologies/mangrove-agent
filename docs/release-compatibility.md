# SDK and local-deployment release compatibility

The normal Agent installation uses the published Python SDKs in
`server/requirements.lock`: MangroveAI 2.2.0 and MangroveMarkets 1.1.1. Their
distribution names are `mangroveai` and `mangrovemarkets`; their canonical imports
are `mangrove_ai` and `mangrove_markets`. The Agent itself is installed from Git
or a repository plugin, not from a published `mangrove-agent` PyPI package.

## Prepared local deployment

As of this source preparation, published MangroveAI SDK 2.2.0 lacks
`client.strategies.local_deployment`. The Agent checks that capability and
refuses the new deployment workflow when it is absent. Updating only the Agent
source, or running an unversioned pip upgrade, does not close that release gap.

The coordinated candidates are Python SDK **2.3.0** and API **7.11.0**. The API
must serve the validated strategy snapshot/local-deployment contract; a version
label or merged source alone is insufficient. The TypeScript SDK **1.2.0** adds
the corresponding `client.strategies.localDeployment` helper for external
clients. The Agent's runtime uses Python, so it needs no npm dependency.

Current default installation remains runnable with published packages. Candidate
compatibility checks use exact reviewed source artifacts separately and do not
make those candidates registry releases. This work does not adopt separately
pending changes to the default risk policy. Mangrove execution software retains
its risk decisions, audit data, error and recovery behavior.

## Complete the release transition

1. Qualify and release the API and SDK from their reviewed main commits, then
   verify the exact serving image and published package artifacts.
2. Update the Agent SDK floor and lock to the qualified published 2.3.0 release.
   Use the dependency regeneration command in `server/requirements.txt`; keep
   unrelated locked packages unchanged.
3. Run the normal Agent CI and an installed-package compatibility check for the
   local-deployment contract. Source mocks alone do not prove installed support.
4. Verify the intended authenticated workflow only under its separate user
   authorization. SDK installation and source CI do not authorize activation,
   broker orders, payments or paid provider calls.

Until those gates are met, report the local-deployment integration as prepared,
not installed, released, deployed or accepted end to end. Existing released
capabilities remain the normal installation path.
