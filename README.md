# bank-mcp

<!-- mcp-name: io.github.pritch56/bank-mcp -->

Unofficial, read-only [MCP](https://modelcontextprotocol.io) server for your own UK bank and card data, via
[TrueLayer](https://truelayer.com) Open Banking. It can read accounts, cards, balances and transactions. It cannot
move money: only read scopes are ever requested.

Not affiliated with TrueLayer or any bank. See [SECURITY.md](SECURITY.md) before linking real accounts.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip).

```bash
git clone https://github.com/pritch56/bank-mcp.git
cd bank-mcp
uv sync
uv run bank-mcp-link --help
```

Or install the package into any environment with `pip install .`; this provides the `bank-mcp` (server) and
`bank-mcp-link` (bank linking) commands.

## Get TrueLayer credentials

1. Create a free account at the [TrueLayer Console](https://console.truelayer.com).
2. Create an application and note its **client ID** and **client secret**. Start with the sandbox, then switch to live.
3. In the application's settings, add this **redirect URI**: `http://localhost:3000/callback`
   (use the same port as `--port` if you change it).
4. Make sure the Data API is enabled with these scopes: `info accounts cards balance transactions offline_access`.
5. Export the credentials in the shell you run `bank-mcp-link` from:

```bash
export TRUELAYER_CLIENT_ID=your-client-id
export TRUELAYER_CLIENT_SECRET=your-client-secret
export TRUELAYER_ENV=sandbox   # or "live" (the default)
```

Never commit these values. `.env` files are git-ignored.

## Link a bank

`bank-mcp-link` opens your bank's sign-in page in the browser and stores the resulting tokens under a nickname of
your choice. You can link several banks.

```bash
# Sandbox (no real bank needed)
uv run bank-mcp-link --provider mock --name test --sandbox

# A real bank: use its TrueLayer provider id, for example ob-nationwide
uv run bank-mcp-link --provider ob-nationwide --name nationwide

uv run bank-mcp-link --list               # linked banks and consent expiry
uv run bank-mcp-link --remove nationwide  # forget a bank
```

If the browser does not open, copy the URL printed in the terminal. Linking waits five minutes for the redirect.

UK Open Banking consent lasts **90 days**. When it lapses, tools return `Reconnect required for '<bank>'`; run
`bank-mcp-link` again for that bank.

Tokens are stored in `~/.bank-mcp/tokens.json`, readable only by you.

## Configure your MCP client

Replace `/path/to/bank-mcp` with your checkout. The server reads the same `TRUELAYER_*` credentials from its
environment, and uses the provider id and environment recorded when each bank was linked.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings > Developer > Edit Config), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "bank": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bank-mcp", "bank-mcp"],
      "env": {
        "TRUELAYER_CLIENT_ID": "your-client-id",
        "TRUELAYER_CLIENT_SECRET": "your-client-secret",
        "TRUELAYER_ENV": "live"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add bank \
  --env TRUELAYER_CLIENT_ID=your-client-id \
  --env TRUELAYER_CLIENT_SECRET=your-client-secret \
  --env TRUELAYER_ENV=live \
  -- uv run --directory /path/to/bank-mcp bank-mcp
```

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

When all banks are queried, the working banks are still returned and the failing ones are listed under `errors`.

## Configuration

| Variable | Meaning |
| --- | --- |
| `TRUELAYER_CLIENT_ID` / `TRUELAYER_CLIENT_SECRET` | TrueLayer Console credentials (required) |
| `TRUELAYER_ENV` | `live` (default) or `sandbox` |
| `TRUELAYER_PROVIDER_ID` | Provider id; set automatically per bank by `bank-mcp-link` |
| `BANK_TOKEN_FILE` | Token file path (default `~/.bank-mcp/tokens.json`) |
| `BANK_MAX_RANGE_DAYS` | Longest date range a single transaction query may span |
| `BANK_TRUELAYER_REFRESH_TOKEN` | Optional refresh-token seed |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Release history is in [CHANGELOG.md](CHANGELOG.md).

## Licence

[MIT](LICENSE)
