# Security review (KAN-27)

Scope: token handling, token file permissions, requested scopes, dependencies.

| Area | Result | Evidence |
|---|---|---|
| Tokens in logs / tool output / errors | Pass | `tests/test_security.py` drives refresh, API, network, token-store and corrupt-file failures and asserts no client secret, access token or refresh token appears in logs, exceptions or MCP tool results. |
| Token file permissions | Pass | POSIX: file 0600, folder not group/other accessible (tests). Windows: ACL has no inherited or broad entries (test runs `icacls`; not run on POSIX CI). |
| Scopes | Pass | Requested scopes `info accounts cards balance transactions offline_access` are asserted to be a subset of read-only scopes; no `payments`. |
| Dependencies | Pass | `pip-audit` on the locked set: no known vulnerabilities. |

## Finding fixed

- **Low:** on an HTTP 400 the upstream `error_description` was echoed into the client-facing error. A misbehaving upstream
  could reflect a token back. `TrueLayerProvider._redact` now blanks any held secret (client secret, seed/refresh/access
  tokens) from that text.

## Residual notes

- The file token store is plaintext JSON protected only by file permissions (documented in `token_store.py`).
- `BANK_TOKEN_STORE=package.module:Class` imports and runs the named code; set it only to trusted modules.
- The Windows ACL test and the POSIX mode tests each skip on the other platform; CI should run both.
