# Contributing

Thanks for your interest in contributing to mangrove-agent.

## Getting Started

1. Fork the repo
2. Clone your fork
3. Create a branch (`git checkout -b feat/your-feature`)
4. Make your changes
5. Run tests: `ENVIRONMENT=test pytest tests/ -v`
6. Run linter: `ruff check src/ tests/`
7. Commit and push
8. Open a pull request

## Development Setup

```bash
cp src/config/local-example-config.json src/config/local-config.json
docker compose up -d --build
ENVIRONMENT=test pytest tests/ -v
ruff check src/ tests/
```

## Code Style

- Python 3.11+
- Linted with [ruff](https://docs.astral.sh/ruff/) (config in `pyproject.toml`)
- All config values come from JSON config files -- no hardcoded values, no env var overrides
- x402 endpoints go under `/api/x402/`, free/auth endpoints under `/api/v1/`
- REST and MCP tools call the same service layer

## Reporting Issues

Open an issue at [github.com/MangroveTechnologies/mangrove-agent/issues](https://github.com/MangroveTechnologies/mangrove-agent/issues).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
