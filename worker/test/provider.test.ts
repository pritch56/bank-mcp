import { exportJWK, generateKeyPair, SignJWT, createLocalJWKSet } from "jose";
import { describe, expect, it } from "vitest";
import { verifyAccess } from "../src/access.js";
import { ConsentExpiredError, ReconnectRequiredError } from "../src/errors.js";
import { BankService } from "../src/service.js";
import { BankTokenStore, KV_KEY } from "../src/store.js";
import { TrueLayerConfig, TrueLayerProvider } from "../src/truelayer.js";

class FakeKV {
  data = new Map<string, string>();
  async get(key: string, type?: string) {
    const v = this.data.get(key);
    return v === undefined ? null : type === "json" ? JSON.parse(v) : v;
  }
  async put(key: string, value: string) {
    this.data.set(key, value);
  }
}
const kvWith = (doc: unknown) => {
  const kv = new FakeKV();
  kv.data.set(KV_KEY, JSON.stringify(doc));
  return kv as unknown as KVNamespace & FakeKV;
};
const j = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const NOW = Date.parse("2026-10-10T12:00:00Z");
const farFuture = "2027-01-01T00:00:00Z";

function setup(handler: (url: URL, init?: RequestInit) => Response, tokens: Record<string, unknown> = { refresh_token: "r-old-token" }) {
  const kv = kvWith({ banks: { card: { tokens, expires_at: farFuture } } });
  const banks = new BankTokenStore(kv);
  const calls: URL[] = [];
  const fetchFn = (async (input: URL | string, init?: RequestInit) => {
    const url = new URL(String(input));
    calls.push(url);
    return handler(url, init);
  }) as typeof fetch;
  const config = new TrueLayerConfig("cid", "csecret-value", "ob-test", "sandbox");
  const provider = new TrueLayerProvider(config, banks.forBank("card"), { fetch: fetchFn, now: () => NOW, backoffMs: 0 });
  return { kv, banks, provider, calls };
}

const api = (url: URL): Response => {
  if (url.pathname === "/connect/token") return j({ access_token: "access-1", refresh_token: "r-new-token", expires_in: 3600 });
  if (url.pathname === "/data/v1/accounts") return j({ results: [] });
  if (url.pathname === "/data/v1/cards") return j({ results: [{ account_id: "c1", display_name: "Visa", currency: "GBP", card_type: "CREDIT", card_network: "visa", partial_card_number: "1234" }] });
  if (url.pathname === "/data/v1/cards/c1/balance") return j({ results: [{ currency: "GBP", current: 120.5, available: 879.5, credit_limit: 1000, update_timestamp: "2026-10-10T10:00:00Z" }] });
  if (url.pathname === "/data/v1/cards/c1/transactions")
    return j({ results: [{ transaction_id: "t", normalised_provider_transaction_id: "p1", timestamp: "2026-10-05T09:00:00+01:00", description: "  COFFEE  ", amount: 3.5, currency: "GBP", transaction_type: "DEBIT", merchant_name: "Cafe" }] });
  if (url.pathname === "/data/v1/cards/c1/transactions/pending")
    return j({ results: [{ timestamp: "2026-10-09T09:00:00Z", description: "TRAIN", amount: 20, currency: "GBP", transaction_type: "DEBIT" }] });
  return j({ error: "not_found" }, 404);
};

describe("TrueLayerProvider", () => {
  it("refreshes, persists the rotated refresh token, and normalises card data", async () => {
    const { provider, kv } = setup(api);
    const accounts = await provider.listAccounts();
    expect(accounts[0]).toMatchObject({ id: "c1", type: "credit_card", card_network: "VISA", last4: "1234" });
    const saved = JSON.parse(kv.data.get(KV_KEY)!);
    expect(saved.banks.card.tokens.refresh_token).toBe("r-new-token");
    expect(saved.banks.card.expires_at).toBe(farFuture); // consent expiry survives a refresh

    const bal = await provider.getBalance("c1");
    expect(bal.balance_minor).toBe(-12050); // owed => negative
    expect(bal.available_credit_minor).toBe(87950);

    const page = await provider.listTransactions("c1", "2026-10-01", "2026-10-11", false);
    expect(page.transactions).toHaveLength(1);
    expect(page.transactions[0]).toMatchObject({ amount_minor: -350, description: "COFFEE", merchant: "Cafe", date: "2026-10-05" });
    expect(page.pendingExcluded).toBe(1);
    const withPending = await provider.listTransactions("c1", "2026-10-01", "2026-10-11", true);
    expect(withPending.transactions.map((t) => t.pending)).toEqual([false, true]);
  });

  it("maps invalid_grant to consent expired", async () => {
    const { provider } = setup((url) => (url.pathname === "/connect/token" ? j({ error: "invalid_grant" }, 400) : api(url)));
    await expect(provider.listAccounts()).rejects.toBeInstanceOf(ConsentExpiredError);
  });

  it("never echoes secrets from upstream 400 text", async () => {
    const { provider } = setup((url) =>
      url.pathname === "/data/v1/accounts" ? j({ error: "bad", error_description: "echo csecret-value" }, 400) : api(url),
    );
    await expect(provider.listAccounts()).rejects.toThrow(/\[redacted\]/);
  });
});

describe("BankService", () => {
  it("returns working banks and lists the failing ones; names the bank when asked for one", async () => {
    const kv = kvWith({
      banks: {
        good: { tokens: { refresh_token: "r-good-token", provider_id: "ob-test", environment: "sandbox" }, expires_at: farFuture },
        lapsed: { tokens: { refresh_token: "r-lapsed-token" }, expires_at: "2020-01-01T00:00:00Z" },
      },
    });
    const banks = new BankTokenStore(kv);
    const svc = new BankService(banks, { TRUELAYER_CLIENT_ID: "cid", TRUELAYER_CLIENT_SECRET: "csecret-value" }, async (n) => {
      const config = new TrueLayerConfig("cid", "csecret-value", "ob-test", "sandbox");
      return new TrueLayerProvider(config, banks.forBank(n), { fetch: (async (u: URL | string) => api(new URL(String(u)))) as typeof fetch, now: () => NOW });
    });
    const all = (await svc.listAccounts()) as unknown as { results: unknown[]; errors: { bank: string; error: string }[] };
    expect(all.results).toHaveLength(1);
    expect(all.errors).toEqual([expect.objectContaining({ bank: "lapsed", error: "reconnect_required" })]);
    await expect(svc.listAccounts("lapsed")).rejects.toBeInstanceOf(ReconnectRequiredError);
    expect(JSON.stringify(await svc.listConnections())).toContain("reconnect_required");
  });
});

describe("Cloudflare Access", () => {
  async function fixture() {
    const { publicKey, privateKey } = await generateKeyPair("RS256");
    const jwks = createLocalJWKSet({ keys: [{ ...(await exportJWK(publicKey)), alg: "RS256", kid: "k" }] });
    const sign = (claims: Record<string, unknown>, aud = "aud123", iss = "https://team.cloudflareaccess.com") =>
      new SignJWT(claims).setProtectedHeader({ alg: "RS256", kid: "k" }).setIssuer(iss).setAudience(aud).setExpirationTime("5m").sign(privateKey);
    return { jwks, sign };
  }
  const env = { ACCESS_TEAM_DOMAIN: "team", ACCESS_AUD: "aud123" };
  const req = (token?: string) => new Request("https://x/mcp", { headers: token ? { "Cf-Access-Jwt-Assertion": token } : {} });

  it("accepts a valid token and rejects everything else", async () => {
    const { jwks, sign } = await fixture();
    expect(await verifyAccess(req(await sign({ email: "me@example.com" })), env, jwks)).toEqual({ ok: true, subject: "me@example.com" });
    expect(await verifyAccess(req(), env, jwks)).toMatchObject({ ok: false, status: 401 });
    expect(await verifyAccess(req(await sign({}, "other")), env, jwks)).toMatchObject({ ok: false, status: 403 });
    expect(await verifyAccess(req(await sign({}, "aud123", "https://evil.cloudflareaccess.com")), env, jwks)).toMatchObject({ ok: false, status: 403 });
    expect(await verifyAccess(req("garbage"), env, jwks)).toMatchObject({ ok: false, status: 403 });
  });
  it("fails closed when not configured", async () => {
    expect(await verifyAccess(req("x"), {})).toMatchObject({ ok: false, status: 503 });
  });
});
