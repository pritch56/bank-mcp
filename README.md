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

Work in progress: server and link helper land in later tasks of the KAN-15 epic.
