# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0]

### Added

- Read-only MCP server with five tools: `list_connections`, `list_accounts`, `get_balance`, `list_transactions`,
  `list_pending`.
- Bank-agnostic TrueLayer provider for accounts and cards (live and sandbox).
- Multiple linked banks, each stored under a nickname in an owner-only token file.
- `bank-mcp-link` command to link, list and remove banks via the TrueLayer browser sign-in (PKCE), with 90-day
  consent expiry tracking.
- Unit tests against mocked TrueLayer, an opt-in sandbox smoke test, and security tests.

### Security

- Upstream error text is redacted so a misbehaving upstream cannot reflect a token back to the client.
