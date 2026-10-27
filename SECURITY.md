# Security policy

## What this server can do

`bank-mcp` is read-only. It requests only the TrueLayer scopes `info accounts cards balance transactions
offline_access` and never `payments`. Tool output contains your financial data, so anything connected to the server
(including the AI model and its host application) can read it.

## Where secrets live

- TrueLayer client ID and secret come from environment variables; never commit them.
- Bank tokens are stored per linked bank in `~/.bank-mcp/tokens.json` as plaintext JSON, protected only by file
  permissions (owner-only on POSIX; an owner-only ACL on Windows). Anyone with access to your user account can read
  them. Use full-disk encryption.
- `BANK_TOKEN_STORE=package.module:Class` imports and runs the named code. Set it only to modules you trust.
- Tokens, client secrets and upstream error text are redacted from logs, errors and tool results.

To revoke access, run `bank-mcp-link --remove <name>` and revoke the consent in your bank's app or website.

## Reporting a vulnerability

Please report privately through GitHub: **Security > Report a vulnerability** at
<https://github.com/pritch56/bank-mcp/security/advisories/new>. Do not open a public issue for security problems, and
never include real tokens or account data in a report. Expect an initial response within a week.

## Review notes

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the latest review of token handling, file permissions, scopes and
dependencies.
