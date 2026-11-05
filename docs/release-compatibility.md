# SDK and local-deployment release compatibility

The normal installation in this Agent source uses the published Python SDKs in
`server/requirements.lock`: MangroveAI 2.2.0 and MangroveMarkets 1.1.1. Their
distribution names are `mangroveai` and `mangrovemarkets`; their canonical imports
are `mangrove_ai` and `mangrove_markets`. The Agent itself is installed from Git
or a repository plugin, not from a published `mangrove-agent` PyPI package.

## Published SDK and prepared local deployment

Published [MangroveAI SDK 2.3.0](https://pypi.org/project/mangroveai/2.3.0/), from
[SDK main 55b09392](https://github.com/MangroveTechnologies/MangroveAI-SDK/commit/55b09392b144ff936080aa4d0199d1e4caa4a985),
already includes `client.strategies.local_deployment`. Standalone SDK HTTP use
is separate from this Agent's locked installation: its normal 2.2.0 SDK lacks
the helper. The Agent checks the installed capability and refuses an unsupported
workflow. Publication did not upgrade the Agent's lock or its installed users.

The next combined Python SDK candidate is **2.4.0**, with TypeScript **1.2.0**.
They are not yet published; use released versions for registry installation.
The helper requires the validated
strategy snapshot/local-deployment contract from API **7.11 or later** actually
serving in the intended deployment. The coordinated API packaging candidate is
now **7.12.0**; that is separate from the helper's minimum capability. A version
label or merged source alone is insufficient. TypeScript 1.2.0 adds
the corresponding `client.strategies.localDeployment` helper for external
clients. The Agent's runtime uses Python, so it needs no npm dependency.

The default installation remains runnable with published packages. Candidate
compatibility checks use exact reviewed source artifacts separately and do not
make those candidates registry releases. This work does not adopt separately
pending changes to the default risk policy. Mangrove execution software retains
its risk decisions, audit data, error and recovery behavior.

## Package coexistence is a separate gate

The published 2.3.0 wheel (SHA256
`94a9e8918f1a7f7e02f58140c94bbe167077b2ac4fbf62df3dfe3358f66a000a`)
still has the legacy `mangroveai/` directory. Its successful standalone SDK
requests do not prove that it can share a case-insensitive filesystem with the
private engine's `MangroveAI/` directory.

[SDK PR83 source a65fadf3](https://github.com/MangroveTechnologies/MangroveAI-SDK/commit/a65fadf32121b62f29baf6b80d55ed443825db4a)
prepares the flat `mangroveai.py` alias in Python 2.4.0. Its actual wheel SHA256
`a08b3b74c86f3b0ca5736a33bcc4fe277cf92e3c36afe332cc5045ece8d4cf4d`
was checked on a real case-insensitive Mac filesystem with private engine
7.12.0 from [API source 06ef0ffe](https://github.com/MangroveTechnologies/MangroveAI/commit/06ef0ffe39fa8693d1b281d3ec9d092bb8ef65dc),
wheel SHA256 `4bd3691eb3355aa9ae01e5dea1a7af196e3284dc78ba2d77e2b265441560422a`.
Both install/import orders and both uninstall-survivor payload checks passed.
The immutable namespace receipt SHA256 is
`9e6b5958f90b09a2c6f4a022d0eaf208d0bc9d03dd78d59c409bd1667827ad7c`.
This check does not qualify the complete API dependency graph, a running image
or an authenticated business workflow.

The combined [SDK PR84 source 9d4b517a](https://github.com/MangroveTechnologies/MangroveAI-SDK/commit/9d4b517a55a401c9034c0706e3c1456949fd3508)
includes the [PR83 packaging repair](https://github.com/MangroveTechnologies/MangroveAI-SDK/pull/83)
and [PR85 contract corrections](https://github.com/MangroveTechnologies/MangroveAI-SDK/pull/85), plus optional
Signal metadata. The Agent's separate prepared-candidate CI builds that exact
2.4.0 source and checks its installed SDK through the Agent adapter. Its source
and artifacts remain distinct from the PR83-only Mac namespace receipt above.

Earlier checks used an unpublished 2.3.0 candidate from
[SDK source 3e1d4ce1](https://github.com/MangroveTechnologies/MangroveAI-SDK/commit/3e1d4ce13df96396f5f19622267bb85d2e6a2f90),
wheel SHA256 `2f94ce710661d7f65c5407551b9ef9a2fa7173f95c3dc380bd6535987eb3ac9b`.
Those historical receipts are not the published 2.3.0 wheel and must not be
relabeled as current 2.4.0 qualification.

## Complete the release transition

1. Integrate [SDK PR83](https://github.com/MangroveTechnologies/MangroveAI-SDK/pull/83),
   [SDK PR85](https://github.com/MangroveTechnologies/MangroveAI-SDK/pull/85), then
   [SDK PR84](https://github.com/MangroveTechnologies/MangroveAI-SDK/pull/84),
   refresh dependent branches and qualify their combined main source. Release
   Python 2.4.0 and TypeScript 1.2.0 through the owning workflows under publication
   authorization, then verify their actual registry artifacts. Separately verify
   the required API contract is serving; do not infer it from a package version.
2. After actual 2.4.0 publication and qualification, update the Agent SDK floor
   and lock to that published release.
   Use the dependency regeneration command in `server/requirements.txt`; keep
   unrelated locked packages unchanged. Change the normal CI capability expectation
   to `present` in the same update; it currently requires the released default
   to lack the helper.
3. Run the normal Agent CI and an installed-package compatibility check for the
   local-deployment contract. Source mocks alone do not prove installed support.
4. Verify the intended authenticated workflow only under its separate user
   authorization. SDK installation and source CI do not authorize activation,
   broker orders, payments or paid provider calls.

Until those gates are met, report the local-deployment integration as prepared,
not installed, released, deployed or accepted end to end. Existing released
capabilities remain the normal installation path.
