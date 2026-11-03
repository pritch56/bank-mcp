/**
 * TrueLayer Data API v1 client (accounts and cards), read-only. Port of bank_mcp.providers.truelayer.
 *
 * TrueLayer card sign convention: POSITIVE amount = money out of the card (purchase),
 * NEGATIVE = money in. This module inverts that so negative always means money out.
 * Bank linking (the authorisation-code exchange) stays in the local `bank-mcp-link` CLI.
 */

import {
  AccountNotFoundError,
  BankMcpError,
  ConfigError,
  ConsentExpiredError,
  InvalidRequestError,
  NotConnectedError,
  NotSupportedError,
  ProviderError,
  RateLimitError,
  ScaRequiredError,
  TokenExpiredError,
  TokenStoreError,
} from "./errors.js";
import {
  addDays,
  cleanText,
  dateInWindow,
  londonDate,
  londonToday,
  makeTransaction,
  mergeTransactions,
  SOURCE,
  stableId,
  toMinor,
  utcToday,
  utcZ,
  type Account,
  type Balance,
  type Transaction,
  type TransactionPage,
} from "./models.js";
import type { TokenDoc, TokenStore } from "./store.js";

export const USER_AGENT = "bank-mcp-worker/0.1.2";
const ENVIRONMENTS = {
  live: ["https://auth.truelayer.com", "https://api.truelayer.com"],
  sandbox: ["https://auth.truelayer-sandbox.com", "https://api.truelayer-sandbox.com"],
} as const;
const KINDS = ["accounts", "cards"] as const;
type Kind = (typeof KINDS)[number];
const RELINK_HINT =
  "Re-authorise the bank by running `bank-mcp-link` on your own machine, then `npm run seed` in worker/.";
const MAX_WINDOW_DAYS = 88;
const MAX_REQUESTS_PER_CALL = 30;
const ID_RE = /^[A-Za-z0-9_-]+$/;
const PROVIDER_ID_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

export interface TrueLayerEnv {
  TRUELAYER_CLIENT_ID?: string;
  TRUELAYER_CLIENT_SECRET?: string;
  TRUELAYER_ENV?: string;
  TRUELAYER_PROVIDER_ID?: string;
  TRUELAYER_WINDOW_DAYS?: string;
}

export class TrueLayerConfig {
  readonly authBase: string;
  readonly apiBase: string;
  readonly providerId: string;
  readonly environment: "live" | "sandbox";
  readonly windowDays: number;

  constructor(
    readonly clientId: string,
    readonly clientSecret: string,
    providerId: string,
    environment = "live",
    windowDays = MAX_WINDOW_DAYS,
  ) {
    if (environment !== "live" && environment !== "sandbox") throw new ConfigError("TRUELAYER_ENV must be 'live' or 'sandbox'.");
    if (!clientId || !clientSecret) {
      throw new ConfigError(
        "TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET must be set (`wrangler secret put ...`). See worker/README.md.",
      );
    }
    const id = (providerId || "").trim().toLowerCase();
    if (!PROVIDER_ID_RE.test(id)) {
      throw new ConfigError("This bank has no TrueLayer provider id recorded. Link it again with `bank-mcp-link` and re-seed.");
    }
    this.providerId = id;
    this.environment = environment;
    [this.authBase, this.apiBase] = ENVIRONMENTS[environment];
    this.windowDays = Math.max(2, Math.min(Math.trunc(windowDays) || MAX_WINDOW_DAYS, MAX_WINDOW_DAYS));
  }

  /** The provider id and environment recorded when the bank was linked win over the Worker's settings. */
  static forBank(env: TrueLayerEnv, tokens: TokenDoc | null): TrueLayerConfig {
    const windowDays = Number(env.TRUELAYER_WINDOW_DAYS || MAX_WINDOW_DAYS);
    if (!Number.isInteger(windowDays)) throw new ConfigError("TRUELAYER_WINDOW_DAYS must be an integer.");
    return new TrueLayerConfig(
      env.TRUELAYER_CLIENT_ID ?? "",
      env.TRUELAYER_CLIENT_SECRET ?? "",
      String(tokens?.provider_id || env.TRUELAYER_PROVIDER_ID || ""),
      String(tokens?.environment || env.TRUELAYER_ENV || "live").trim().toLowerCase(),
      windowDays,
    );
  }
}

class Budget {
  spent = 0;
  constructor(readonly limit = MAX_REQUESTS_PER_CALL) {}
  spend(): void {
    if (this.spent >= this.limit) {
      throw new ProviderError(
        `Stopped after ${this.limit} upstream requests for one call (safety limit). Ask for a shorter date range, or retry later if the provider is failing.`,
      );
    }
    this.spent += 1;
  }
}

type Row = Record<string, unknown>;
const isRow = (v: unknown): v is Row => typeof v === "object" && v !== null && !Array.isArray(v);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export interface ProviderOptions {
  fetch?: typeof fetch;
  maxRetries?: number;
  backoffMs?: number;
  now?: () => number;
}

export class TrueLayerProvider {
  readonly name = "truelayer";
  private tokens: TokenDoc | null = null;
  private unsaved = false;
  private storeError: string | null = null;
  private refreshing: Promise<string> | null = null;
  private idOwners = new Map<string, [string, string]>();
  private kinds = new Map<string, Kind>();
  private readonly doFetch: typeof fetch;
  private readonly maxRetries: number;
  private readonly backoffMs: number;
  private readonly now: () => number;

  constructor(
    readonly config: TrueLayerConfig,
    private readonly store: TokenStore,
    opts: ProviderOptions = {},
  ) {
    this.doFetch = opts.fetch ?? ((...a) => fetch(...a));
    this.maxRetries = opts.maxRetries ?? 2;
    this.backoffMs = opts.backoffMs ?? 1000;
    this.now = opts.now ?? Date.now;
  }

  pendingWarning(): string | null {
    return this.storeError ? `[${TokenStoreError.code}] ${this.storeError}` : null;
  }

  // ------------------------------------------------------------------ tokens

  /** Blank out any secret we hold before upstream text is shown to a client. */
  private redact(text: string): string {
    const held = [this.config.clientSecret, ...Object.values(this.tokens ?? {})];
    for (const s of held) if (typeof s === "string" && s.length >= 8) text = text.split(s).join("[redacted]");
    return text;
  }

  private async save(tokens: TokenDoc): Promise<boolean> {
    try {
      await this.store.save(tokens);
    } catch (e) {
      const detail = e instanceof BankMcpError ? e.detail : `${(e as Error).name} from the token store.`;
      this.unsaved = true;
      this.storeError =
        `The refreshed tokens could not be saved: ${detail} They are held in memory only; if this ` +
        "request ends before a save succeeds, the bank may have to be linked again.";
      return false;
    }
    this.unsaved = false;
    this.storeError = null;
    return true;
  }

  private async loadTokens(): Promise<TokenDoc> {
    if (this.unsaved && this.tokens) {
      await this.save(this.tokens);
      return this.tokens;
    }
    let tokens: TokenDoc | null;
    try {
      tokens = await this.store.load();
    } catch (e) {
      if (this.tokens?.refresh_token) return this.tokens;
      if (e instanceof BankMcpError) throw e;
      throw new TokenStoreError(`The token store could not be read (${(e as Error).name}).`);
    }
    if (tokens?.refresh_token) {
      this.tokens = tokens;
      return tokens;
    }
    throw new NotConnectedError(`No bank connection has been set up yet (no refresh token in the token store). ${RELINK_HINT}`);
  }

  private async tokenRequest(payload: Record<string, string>): Promise<Row> {
    let resp: Response;
    try {
      resp = await this.doFetch(`${this.config.authBase}/connect/token`, {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      throw new ProviderError(`Could not reach TrueLayer's token endpoint (${(e as Error).name}).`);
    }
    const body = await json(resp);
    if (resp.status === 200) return body;
    const error = String(body.error ?? "");
    if (error === "invalid_grant") {
      throw new ConsentExpiredError(
        "TrueLayer rejected the refresh token (invalid_grant): the bank consent has expired, been revoked, or the token was superseded. " +
          RELINK_HINT,
      );
    }
    if (error === "invalid_client" || error === "unauthorized_client") {
      throw new ConfigError(
        `TrueLayer rejected the client credentials (${error}). Check TRUELAYER_CLIENT_ID / TRUELAYER_CLIENT_SECRET and that TRUELAYER_ENV matches the credentials (live vs sandbox).`,
      );
    }
    if (resp.status === 429) throw new RateLimitError("TrueLayer's token endpoint is rate limiting requests. Try again later.", retryAfter(resp));
    throw new ProviderError(`TrueLayer token endpoint returned HTTP ${resp.status} (${error || "no error code"}).`);
  }

  private async persist(previous: TokenDoc, body: Row): Promise<TokenDoc> {
    const tokens: TokenDoc = { ...previous, access_token: body.access_token };
    const lifetime = Number(body.expires_in || 3600);
    tokens.access_token_expires_at = this.now() / 1000 + (Number.isFinite(lifetime) ? lifetime : 3600);
    // Refresh tokens may rotate: keep the newest, or the old one if none is returned.
    if (body.refresh_token) tokens.refresh_token = body.refresh_token;
    tokens.updated_at = new Date(this.now()).toISOString().replace(/\.\d{3}Z$/, "Z");
    this.tokens = tokens; // usable from now on, whether or not the save works
    await this.save(tokens);
    return tokens;
  }

  private async refresh(tokens: TokenDoc): Promise<TokenDoc> {
    const body = await this.tokenRequest({
      grant_type: "refresh_token",
      client_id: this.config.clientId,
      client_secret: this.config.clientSecret,
      refresh_token: String(tokens.refresh_token),
    });
    if (!body.access_token) throw new ProviderError("TrueLayer's token response did not contain an access token.");
    return this.persist(tokens, body);
  }

  /** A usable access token. `rejected` is one the API just answered 401 to. Calls are serialised. */
  private async accessToken(rejected?: string): Promise<string> {
    while (this.refreshing) await this.refreshing.catch(() => undefined);
    const run = (async () => {
      const tokens = await this.loadTokens();
      const current = tokens.access_token;
      const expiresAt = Number(tokens.access_token_expires_at || 0) || 0;
      if (current && expiresAt - 60 > this.now() / 1000 && current !== rejected) return String(current);
      return String((await this.refresh(tokens)).access_token);
    })();
    this.refreshing = run;
    try {
      return await run;
    } finally {
      this.refreshing = null;
    }
  }

  // ---------------------------------------------------------------- requests

  private async get(path: string, params?: Record<string, string>, budget = new Budget()): Promise<Row[]> {
    const url = new URL(`${this.config.apiBase}${path}`);
    for (const [k, v] of Object.entries(params ?? {})) url.searchParams.set(k, v);
    let refreshed = false;
    let attempt = 0;
    let token = await this.accessToken();
    for (;;) {
      budget.spend();
      let resp: Response;
      try {
        resp = await this.doFetch(url, { headers: headers(token) });
      } catch (e) {
        if (attempt < this.maxRetries) {
          attempt += 1;
          await sleep(this.backoffMs * attempt);
          continue;
        }
        throw new ProviderError(`Could not reach TrueLayer (${(e as Error).name}).`);
      }
      const body = await json(resp);
      const status = resp.status;
      const error = cleanText(body.error ?? "").slice(0, 60);
      if (status === 200) {
        const results = body.results;
        if (!Array.isArray(results) || !results.every(isRow)) {
          throw new ProviderError("Unexpected response from TrueLayer (not JSON with a 'results' list).");
        }
        return results;
      }
      if (status === 401) {
        if (!refreshed) {
          refreshed = true;
          token = await this.accessToken(token);
          continue;
        }
        throw new TokenExpiredError(
          `TrueLayer rejected the access token even after refreshing it (${error || "unauthorized"}). ${RELINK_HINT}`,
        );
      }
      if (status === 403) {
        if (error === "sca_exceeded") {
          throw new ScaRequiredError(
            "The bank requires a fresh authentication for this request (sca_exceeded). This happens when asking for history older than about 90 days long after linking. Request a more recent range, or re-link the bank and fetch the older history within 5 minutes.",
          );
        }
        throw new ConsentExpiredError(
          `Access to the bank data was denied (${error || "access_denied"}): the consent has expired or been revoked. ${RELINK_HINT}`,
        );
      }
      if (status === 404) {
        throw new AccountNotFoundError(
          "TrueLayer does not know this account (account_not_found). Call list_accounts for current ids - they can change after the bank is re-linked.",
        );
      }
      if (status === 429) {
        const after = retryAfter(resp);
        throw new RateLimitError(
          `Rate limited (${error || "too_many_requests"}). Banks allow only a few unattended data fetches per day per endpoint. Do not retry immediately` +
            (after ? `; retry after ${after}s.` : "; retry in 6 hours or more."),
          after,
        );
      }
      if (status === 400) {
        const detail = this.redact(cleanText(body.error_description ?? "").slice(0, 200));
        if (error === "invalid_date_range") throw new InvalidRequestError(`TrueLayer rejected the date range (invalid_date_range). ${detail}`);
        throw new InvalidRequestError(`TrueLayer rejected the request (${error || "bad_request"}). ${detail}`);
      }
      if (status === 501) {
        throw new NotSupportedError(`The bank does not support this endpoint via TrueLayer (${error || "endpoint_not_supported"}).`);
      }
      if (status >= 500) {
        if (attempt < this.maxRetries) {
          attempt += 1;
          await sleep(this.backoffMs * attempt);
          continue;
        }
        throw new ProviderError(
          `TrueLayer / the bank is temporarily unavailable (HTTP ${status}, ${error || "no error code"}). Safe to retry later.`,
        );
      }
      throw new ProviderError(`Unexpected HTTP ${status} from TrueLayer (${error || "no error code"}).`);
    }
  }

  // ------------------------------------------------------------------- tools

  async listAccounts(): Promise<Account[]> {
    const found: Account[] = [];
    const kinds = new Map<string, Kind>();
    let unsupported: NotSupportedError | null = null;
    let skipped = 0;
    for (const kind of KINDS) {
      let rows: Row[];
      try {
        rows = await this.get(`/data/v1/${kind}`);
      } catch (e) {
        if (e instanceof NotSupportedError) {
          unsupported = e;
          skipped += 1;
          continue;
        }
        throw e;
      }
      try {
        for (const row of rows) {
          const account = this.account(row, kind);
          found.push(account);
          kinds.set(account.account_id, kind);
        }
      } catch {
        throw new ProviderError(`TrueLayer returned an item from /${kind} in an unexpected shape.`);
      }
    }
    if (unsupported && skipped === KINDS.length) throw unsupported;
    this.kinds = kinds;
    return found;
  }

  private async kindOf(accountId: string): Promise<Kind> {
    if (!this.kinds.has(accountId)) await this.listAccounts();
    const kind = this.kinds.get(accountId);
    if (!kind) {
      throw new AccountNotFoundError(
        "TrueLayer does not know this account (account_not_found). Call list_accounts for current ids - they can change after the account is re-linked.",
      );
    }
    return kind;
  }

  private account(c: Row, kind: Kind): Account {
    const id = String(c.account_id ?? (() => { throw new TypeError("account_id"); })());
    const base = { id, account_id: id, currency: String(c.currency || "GBP").toUpperCase(), provider: this.name, source: SOURCE };
    if (kind === "accounts") {
      const number = isRow(c.account_number) ? c.account_number : {};
      const digits = String(number.number || "");
      return {
        ...base,
        name: cleanText(c.display_name) || "Bank account",
        type: String(c.account_type ?? "").toUpperCase() === "SAVINGS" ? "savings_account" : "current_account",
        card_network: null,
        last4: digits ? digits.slice(-4) : null,
      };
    }
    return {
      ...base,
      name: cleanText(c.display_name) || "Card",
      type: String(c.card_type ?? "").toUpperCase() === "CHARGE" ? "charge_card" : "credit_card",
      card_network: String(c.card_network || "").toUpperCase() || null,
      last4: c.partial_card_number ? String(c.partial_card_number).slice(-4) : null,
    };
  }

  async getBalance(accountId: string): Promise<Balance> {
    checkAccountId(accountId);
    const kind = await this.kindOf(accountId);
    const results = await this.get(`/data/v1/${kind}/${accountId}/balance`);
    const b = results[0];
    if (!b) throw new ProviderError("TrueLayer returned no balance for this account.");
    const cur = String(b.currency || "GBP").toUpperCase();
    const minor = (key: string, negate = false): number | null => {
      if (b[key] === null || b[key] === undefined) return null;
      const v = toMinor(b[key], cur);
      return negate ? (v === 0 ? 0 : -v) : v;
    };
    const day = (key: string): string | null => {
      if (!b[key]) return null;
      try {
        return londonDate(b[key]);
      } catch {
        return null;
      }
    };
    try {
      if (kind === "accounts") {
        return {
          account_id: accountId,
          currency: cur,
          balance_minor: minor("current"),
          available_credit_minor: minor("available"),
          credit_limit_minor: minor("overdraft"),
          last_statement_balance_minor: null,
          last_statement_date: null,
          payment_due_minor: null,
          payment_due_date: null,
          as_of: utcZ(b.update_timestamp),
          notes:
            "balance_minor excludes pending transactions; available_credit_minor is the available balance and credit_limit_minor the overdraft limit",
          source: SOURCE,
        };
      }
      return {
        account_id: accountId,
        currency: cur,
        // ASSUMPTION: `current` = amount spent on the card (positive = owed). Ours: negative = owed.
        balance_minor: minor("current", true),
        available_credit_minor: minor("available"),
        credit_limit_minor: minor("credit_limit"),
        last_statement_balance_minor: minor("last_statement_balance", true),
        last_statement_date: day("last_statement_date"),
        payment_due_minor: minor("payment_due"),
        payment_due_date: day("payment_due_date"),
        as_of: utcZ(b.update_timestamp),
        notes: "balance_minor excludes pending transactions",
        source: SOURCE,
      };
    } catch {
      throw new ProviderError("TrueLayer returned a balance in an unexpected shape.");
    }
  }

  async listTransactions(accountId: string, since: string, before: string, includePending = false): Promise<TransactionPage> {
    checkAccountId(accountId);
    const kind = await this.kindOf(accountId);
    // "to" in the future is rejected, so today is the earlier of London's and UTC's date.
    const now = new Date(this.now());
    const today = [londonToday(now), utcToday(now)].sort()[0]!;
    // One day of padding before `since`: a late-UTC timestamp can fall on `since` in London time.
    const start = addDays(since, -1);
    const end = before < today ? before : today;
    const wins = start <= end ? windows(start, end, this.config.windowDays) : [];
    const budget = new Budget();
    if (wins.length + 1 > budget.limit) {
      throw new InvalidRequestError(
        `This range needs ${wins.length + 1} upstream requests; the limit per call is ${budget.limit}. Ask for a shorter range.`,
      );
    }

    const merged = new Map<string, Transaction>();
    // Consecutive windows share one day; repeated rows collapse by id.
    for (const [from, to] of wins) {
      const raw = await this.get(`/data/v1/${kind}/${accountId}/transactions`, { from, to }, budget);
      mergeTransactions(this.normalise(raw, accountId, false, kind), merged);
    }
    const page: TransactionPage = {
      transactions: [...merged.values()].filter((t) => dateInWindow(t.date, since, before)),
      pendingExcluded: 0,
      earliestPendingDate: null,
      pendingSupported: true,
    };

    // Pending items are always fetched, so the ones left out can be reported.
    let pendingRaw: Row[] = [];
    try {
      pendingRaw = await this.get(`/data/v1/${kind}/${accountId}/transactions/pending`, undefined, budget);
    } catch (e) {
      if (!(e instanceof NotSupportedError)) throw e;
      page.pendingSupported = false;
    }
    const pending = mergeTransactions(this.normalise(pendingRaw, accountId, true, kind));
    const inWindow = [...pending.values()].filter((t) => dateInWindow(t.date, since, before));
    if (includePending) page.transactions.push(...inWindow);
    else page.pendingExcluded = inWindow.length;
    page.earliestPendingDate = inWindow.map((t) => t.date).sort()[0] ?? null;
    page.transactions.sort((a, b) => cmp(a.date, b.date) || Number(a.pending) - Number(b.pending) || cmp(a.id, b.id));
    return page;
  }

  private normalise(raw: Row[], accountId: string, pending: boolean, kind: Kind): Transaction[] {
    try {
      return this.normaliseRows(raw, accountId, pending, kind);
    } catch {
      throw new ProviderError("TrueLayer returned a transaction in an unexpected shape.");
    }
  }

  private normaliseRows(raw: Row[], accountId: string, pending: boolean, kind: Kind): Transaction[] {
    const seen = new Map<string, number>();
    const out: Transaction[] = [];
    for (const r of raw) {
      const currency = String(r.currency || "GBP").toUpperCase();
      const signed = toMinor(r.amount ?? 0, currency);
      const direction = String(r.transaction_type || "").toUpperCase();
      let amount: number;
      if (direction === "DEBIT") amount = -Math.abs(signed);
      else if (direction === "CREDIT") amount = Math.abs(signed);
      else if (kind === "accounts") amount = signed; // positive = money in
      else amount = signed === 0 ? 0 : -signed; // TrueLayer cards: positive = money out
      const day = londonDate(r.timestamp);
      const description = cleanText(r.description);
      const meta = isRow(r.meta) ? r.meta : {};
      const content = [day, amount, currency, description] as const;

      const providerId = r.normalised_provider_transaction_id || r.provider_transaction_id;
      let id: string;
      if (providerId && !pending) {
        // Independent of TrueLayer's account_id, which may change on re-link.
        id = this.claimId(stableId("tl", providerId), accountId, content);
      } else {
        // `transaction_id` is unstable between requests; derive the id from the content instead.
        const key = ["tl-hash", accountId, pending ? "pending" : "booked", ...content];
        const k = JSON.stringify(key);
        const n = seen.get(k) ?? 0;
        seen.set(k, n + 1);
        id = stableId(...key, n);
      }

      const classification = (Array.isArray(r.transaction_classification) ? r.transaction_classification : [])
        .filter(Boolean)
        .map(cleanText);
      const category =
        classification.join(" > ") || cleanText(meta.provider_transaction_category) || null;

      const notes: string[] = [];
      const tlCategory = cleanText(r.transaction_category).toUpperCase();
      if (tlCategory && tlCategory !== "PURCHASE" && tlCategory !== "UNKNOWN") notes.push(`type=${tlCategory}`);
      for (const [k, v] of Object.entries(meta)) {
        if (v !== null && v !== undefined && v !== "" && ["foreign", "exchange", "original", "fx"].some((s) => k.toLowerCase().includes(s))) {
          notes.push(`${cleanText(k)}=${cleanText(v)}`);
        }
      }
      if (pending) notes.push("pending: amount and id may change when the charge is booked");

      out.push(
        makeTransaction({
          id,
          account_id: accountId,
          date: day,
          amount_minor: amount,
          currency,
          description,
          merchant: cleanText(r.merchant_name) || null,
          category,
          pending,
          notes: notes.join("; ") || null,
        }),
      );
    }
    return out;
  }

  /** Guard against two accounts on one consent reusing a provider transaction id. */
  private claimId(txnId: string, accountId: string, content: readonly unknown[]): string {
    const key = JSON.stringify(content);
    const owner = this.idOwners.get(txnId);
    if (owner && owner[0] !== accountId && owner[1] !== key) return stableId("dup", txnId, ...content, 0);
    if (this.idOwners.size >= 200_000) this.idOwners.clear();
    this.idOwners.set(txnId, [accountId, key]);
    return txnId;
  }
}

const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

/** Inclusive date windows of at most `days` days covering start..end, each starting on the last day of the previous. */
export function windows(start: string, end: string, days: number): [string, string][] {
  days = Math.max(2, days);
  const out: [string, string][] = [];
  let cur = start;
  for (;;) {
    const candidate = addDays(cur, days - 1);
    const winEnd = candidate < end ? candidate : end;
    out.push([cur, winEnd]);
    if (winEnd >= end) return out;
    cur = winEnd;
  }
}

function checkAccountId(id: string): void {
  if (typeof id !== "string" || !ID_RE.test(id) || id.length > 128) {
    throw new InvalidRequestError("account_id is missing or malformed. Use an id returned by list_accounts.");
  }
}

function headers(token?: string): Record<string, string> {
  const h: Record<string, string> = { Accept: "application/json", "User-Agent": USER_AGENT };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function json(resp: Response): Promise<Row> {
  try {
    const body: unknown = await resp.json();
    return isRow(body) ? body : {};
  } catch {
    return {};
  }
}

function retryAfter(resp: Response): number | null {
  const v = resp.headers.get("Retry-After");
  const n = v === null ? NaN : Number.parseInt(v, 10);
  return Number.isNaN(n) ? null : n;
}
