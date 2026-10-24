# bank-mcp

Unofficial, read-only MCP server for your own UK bank and card data via TrueLayer Open Banking.

The TrueLayer provider is bank-agnostic: set `TRUELAYER_PROVIDER_ID` (e.g. `ob-nationwide`, or `mock` in the
sandbox) and it reads both `/data/v1/accounts` and `/data/v1/cards`.

| Variable | Meaning |
| --- | --- |
| `TRUELAYER_CLIENT_ID` / `TRUELAYER_CLIENT_SECRET` | TrueLayer Console credentials |
| `TRUELAYER_PROVIDER_ID` | Provider id of your bank (required) |
| `TRUELAYER_ENV` | `live` (default) or `sandbox` |
| `BANK_TRUELAYER_REFRESH_TOKEN` | Optional refresh-token seed |

## Tools

`bank-mcp` (stdio) exposes five read-only tools. Each except `list_connections` takes an optional `bank`
nickname; leave it out to query every linked bank.

| Tool | Returns |
| --- | --- |
| `list_connections` | Linked banks and consent expiry |
| `list_accounts` | Accounts and cards |
| `get_balance` | Balances (optionally one `account_id`) |
| `list_transactions` | `since`/`before` dates, `limit`/`offset` paging, `include_pending` |
| `list_pending` | Pending transactions |

A lapsed or revoked consent returns `Reconnect required for '<bank>'`; re-run `bank-mcp-link` for that bank.
When all banks are queried, the working banks are still returned and the failing ones are listed under `errors`.
