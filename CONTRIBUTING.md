# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/pritch56/bank-mcp.git
cd bank-mcp
uv sync
```

## Checks

CI runs these on Linux and Windows; run them before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests run against mocked TrueLayer and need no credentials. `tests/test_sandbox_smoke.py` is opt-in and needs
sandbox credentials plus a linked sandbox bank; it is skipped by default.

## Guidelines

- Keep the server read-only. Do not add write or payment scopes or tools.
- Never commit secrets, tokens or real account data, including in tests and fixtures.
- Add or update tests for every behaviour change, and add a line to the `Unreleased` section of
  [CHANGELOG.md](CHANGELOG.md).
- Keep pull requests small and focused.

Report security problems privately, as described in [SECURITY.md](SECURITY.md).
