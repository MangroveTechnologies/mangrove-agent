# Contributing

Thanks for your interest in contributing to mangrove-agent. This page covers how to propose
a change. For how the code is organized and how to add a route, tool, migration or config
key, see the [extension guide](docs/contributing.md).

## Getting started

1. Fork the repository and clone your fork.
2. Create a branch off `main`, for example `git checkout -b feat/your-feature`.
3. Make your change, with tests.
4. Run the checks below.
5. Commit, push, and open a pull request against `main`.

Keep each pull request to one concern. Do not commit to `main` directly. Pull requests are
reviewed by the engineering team listed in `.github/CODEOWNERS`.

## Development setup

The agent needs Python 3.11 or later. From the repository root:

```bash
./scripts/setup.sh
```

This creates `server/src/config/local-config.json` from the example, generates a local API
key, installs dependencies into `.venv`, starts the agent on port 9080 and registers it with
Claude Code. Run `./scripts/setup.sh --help` for the options, including `--docker` to use
Docker Compose instead of a virtual environment and `--no-mcp` to skip MCP registration.

To run the checks without setting up an agent, install the locked dependencies, which include
`pytest` and `ruff`:

```bash
pip install -r server/requirements.lock
```

## Checks

Run these from the repository root. CI runs the same commands on every pull request.

```bash
ruff check server/src/
python -B scripts/mcp_contract.py
cd server && ENVIRONMENT=test pytest tests/ -v
```

The second command is the offline MCP contract check. If your change adds or alters a tool,
see [MCP contracts](docs/mcp-contracts.md) for how to update the reviewed snapshots.

## Code style

- Python 3.11 or later, linted with [ruff](https://docs.astral.sh/ruff/); the configuration is
  in `server/pyproject.toml`.
- Configuration values come from the per-environment JSON files in `server/src/config/`.
  Do not hardcode them, and do not add environment-variable overrides for them. See the
  [configuration guide](docs/configuration.md).
- Free and authenticated routes live under `/api/v1/`; x402 payment-gated routes live under
  `/api/x402/`.
- REST routes and MCP tools call the same service layer. Do not duplicate business logic.

## Security

Never put a private key, seed phrase, API key or token in a commit, an issue, a test fixture or
a pull request. When describing a vulnerability, leave out real keys and wallet addresses that
hold funds.

## Reporting issues

Open an issue at [github.com/MangroveTechnologies/mangrove-agent/issues](https://github.com/MangroveTechnologies/mangrove-agent/issues).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
