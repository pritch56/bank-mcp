# bank-mcp on Cloudflare Workers

Hosts the same five read-only tools as the Python server (`list_connections`, `list_accounts`, `get_balance`,
`list_transactions`, `list_pending`) as a remote MCP server at `https://<your-domain>/mcp` (Streamable HTTP,
stateless). It is a TypeScript port of the TrueLayer client, because Workers have no filesystem and cannot run the
Python package.

- **Auth:** the Worker is meant to sit behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/).
  It verifies the Access JWT (signature, issuer, audience) on every request and **refuses to serve if Access is not
  configured**. `workers.dev` and preview URLs are disabled.
- **Tokens:** stored in Workers KV (key `tokens`, the same JSON as `~/.bank-mcp/tokens.json`). The Worker writes
  rotated refresh tokens back to KV.
- **Linking** stays local: run `bank-mcp-link` on your machine, then upload the result with `npm run seed`.
  TrueLayer redirects to `localhost`, which a Worker cannot receive.

Read [SECURITY.md](../SECURITY.md) first: this puts bank tokens in Cloudflare KV, which is not encrypted with a key
you control, and the Worker will hold your TrueLayer client secret.

## Deploy

Requires Node 20+ and a Cloudflare account with a domain on Cloudflare (Access needs a hostname you control).

```bash
cd worker
npm install
```

1. **KV namespace:** nothing to do. `wrangler.jsonc` (at the repo root) declares the `TOKENS` binding without an id, and
   wrangler creates the namespace on the first deploy.

2. **Set the TrueLayer credentials** as secrets (and `TRUELAYER_ENV` in the root `wrangler.jsonc` if you use the sandbox):

   ```bash
   npx wrangler secret put TRUELAYER_CLIENT_ID
   npx wrangler secret put TRUELAYER_CLIENT_SECRET
   ```

3. **Add a custom domain route** in `wrangler.jsonc`, for example
   `"routes": [{ "pattern": "bank-mcp.example.com", "custom_domain": true }]`, then deploy once:

   ```bash
   npm run deploy
   ```

   You can also connect the repo to Cloudflare (Workers Builds) with the default settings: its `npx wrangler deploy`
   from the repo root picks up the root `wrangler.jsonc`, which installs `worker/`'s dependencies itself. Secrets and
   the Access vars must still be set, and `workers_dev` is off, so the Worker is reachable only through your route.

4. **Protect it with Access.** In Zero Trust go to Access > Applications > Add > Self-hosted, use the same hostname,
   and add a policy that allows only you (or a service token for headless clients). Copy the application's
   **Audience (AUD) tag** and your **team name** (`<team>.cloudflareaccess.com`) into `wrangler.jsonc`:

   ```jsonc
   "vars": { "ACCESS_TEAM_DOMAIN": "your-team", "ACCESS_AUD": "the-aud-tag" }
   ```

   and deploy again. Until both are set every request returns 503.

5. **Link a bank locally and upload it** (see the main [README](../README.md) for the TrueLayer setup):

   ```bash
   uv run bank-mcp-link --provider ob-nationwide --name nationwide
   npm run seed -- --bank nationwide
   ```

   `seed` merges into what is already in KV. Upload a bank only right after linking it: the Worker rotates refresh
   tokens in KV, so re-uploading an old local copy would replace a newer token with a stale one. When consent lapses
   (every 90 days), tools say `Reconnect required for '<bank>'`; relink and seed again.

## Connect a client

The endpoint is `https://bank-mcp.example.com/mcp`. Clients must pass Access.

- **Claude Code**, with an Access service token (create one in Zero Trust > Access > Service auth, and add a
  *Service Auth* policy to the application):

  ```bash
  claude mcp add --transport http bank https://bank-mcp.example.com/mcp \
    --header "CF-Access-Client-Id: <id>.access" \
    --header "CF-Access-Client-Secret: <secret>"
  ```

- **Clients that speak OAuth** (for example claude.ai custom connectors): enable *Managed OAuth* on the Access
  application so Access acts as the authorization server for MCP clients. See the
  [Cloudflare docs](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/saas-mcp/).

## Configuration

| Name | Kind | Meaning |
| --- | --- | --- |
| `TRUELAYER_CLIENT_ID`, `TRUELAYER_CLIENT_SECRET` | secrets | TrueLayer Console credentials (required) |
| `TRUELAYER_ENV` | var | `live` (default) or `sandbox`; a bank's recorded environment wins |
| `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD` | vars | Cloudflare Access team name and application AUD tag (required) |
| `BANK_MAX_RANGE_DAYS` | var | Longest date range one transaction query may span (default 366) |
| `TRUELAYER_WINDOW_DAYS` | var | Days per upstream transaction request (default and maximum 88) |
| `TOKENS` | KV binding | Token store |

## Develop

```bash
npm run typecheck
npm test
npm run dev   # local; set ACCESS_* in ../.dev.vars (repo root), which is git-ignored
```

## Limits

- KV is eventually consistent. Two simultaneous calls that both need to refresh a bank's token could race, and the
  loser's rotated refresh token may be superseded. Calls are sequential in normal use; if a bank ever reports
  `consent_expired` unexpectedly, relink it.
- Each request builds its own MCP server (stateless), so there are no sessions and no server-initiated messages.
