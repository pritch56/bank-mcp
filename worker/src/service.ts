/** The logic behind the five tools, free of MCP so it can be tested directly (port of BankService). */

import {
  BankMcpError,
  ConsentExpiredError,
  InvalidRequestError,
  NotConnectedError,
  ReconnectRequiredError,
  TokenExpiredError,
} from "./errors.js";
import { resolveWindow, type Transaction } from "./models.js";
import { BankTokenStore, normaliseNickname } from "./store.js";
import { TrueLayerConfig, TrueLayerProvider, type TrueLayerEnv } from "./truelayer.js";

export const DEFAULT_MAX_RANGE_DAYS = 366;
export const DEFAULT_PAGE_SIZE = 100;
export const MAX_PAGE_SIZE = 500;

type Row = Record<string, unknown>;
export type ProviderFactory = (nickname: string) => Promise<TrueLayerProvider>;

export interface ServiceEnv extends TrueLayerEnv {
  BANK_MAX_RANGE_DAYS?: string;
}

export function truelayerFactory(banks: BankTokenStore, env: TrueLayerEnv): ProviderFactory {
  return async (nickname) => {
    const tokens = await banks.load(nickname);
    return new TrueLayerProvider(TrueLayerConfig.forBank(env, tokens), banks.forBank(nickname));
  };
}

export class BankService {
  readonly maxRangeDays: number;
  private readonly factory: ProviderFactory;

  constructor(
    readonly banks: BankTokenStore,
    env: ServiceEnv,
    factory?: ProviderFactory,
  ) {
    this.factory = factory ?? truelayerFactory(banks, env);
    const n = Number(env.BANK_MAX_RANGE_DAYS || DEFAULT_MAX_RANGE_DAYS);
    this.maxRangeDays = Number.isInteger(n) && n > 0 ? n : DEFAULT_MAX_RANGE_DAYS;
  }

  private async targets(bank?: string | null): Promise<string[]> {
    const names = await this.banks.nicknames();
    if (bank !== undefined && bank !== null && String(bank).trim()) {
      const name = normaliseNickname(bank);
      if (!names.includes(name)) {
        throw new InvalidRequestError(`No bank is linked as '${name}'. Linked banks: ${names.join(", ") || "none"}.`);
      }
      return [name];
    }
    if (!names.length) {
      throw new NotConnectedError("No banks are linked yet. Run `bank-mcp-link --provider <id> --name <nickname>`, then `npm run seed` in worker/.");
    }
    return names;
  }

  private async forEach(
    bank: string | null | undefined,
    work: (nickname: string, provider: TrueLayerProvider) => Promise<Row[]>,
  ): Promise<{ banks_queried: string[]; results: Row[]; errors: Row[] }> {
    const targets = await this.targets(bank);
    const results: Row[] = [];
    const errors: Row[] = [];
    for (const nickname of targets) {
      try {
        if (await this.banks.isExpired(nickname)) throw new ReconnectRequiredError(nickname);
        const provider = await this.factory(nickname);
        const got = await work(nickname, provider);
        for (const row of got) results.push({ bank: nickname, ...row });
      } catch (e) {
        if (!(e instanceof BankMcpError)) throw e;
        const needsRelink =
          e instanceof ReconnectRequiredError ||
          e instanceof ConsentExpiredError ||
          e instanceof TokenExpiredError ||
          e instanceof NotConnectedError;
        const failure = needsRelink && !(e instanceof ReconnectRequiredError) ? new ReconnectRequiredError(nickname) : e;
        if (bank !== undefined && bank !== null && String(bank).trim()) throw failure;
        errors.push({ bank: nickname, error: failure.code, message: failure.detail });
      }
    }
    return { banks_queried: targets, results, errors };
  }

  async listConnections(now: Date = new Date()): Promise<Row> {
    const connections: Row[] = [];
    for (const nickname of await this.banks.nicknames()) {
      const stamp = await this.banks.expiresAt(nickname);
      const expired = await this.banks.isExpired(nickname, now);
      const days = stamp ? Math.floor((Date.parse(stamp) - now.getTime()) / 86_400_000) : null;
      const entry: Row = {
        bank: nickname,
        status: expired ? "reconnect_required" : "linked",
        consent_expires_at: stamp,
        days_remaining: days,
      };
      if (expired) entry.message = new ReconnectRequiredError(nickname).detail;
      connections.push(entry);
    }
    return { connections };
  }

  listAccounts(bank?: string | null) {
    return this.forEach(bank, async (_, p) => (await p.listAccounts()) as unknown as Row[]);
  }

  getBalance(bank?: string | null, accountId?: string | null) {
    return this.forEach(bank, async (_, p) => {
      const ids = accountId ? [accountId] : (await p.listAccounts()).map((a) => a.account_id);
      const out: Row[] = [];
      for (const id of ids) out.push((await p.getBalance(id)) as unknown as Row);
      return out;
    });
  }

  async listTransactions(
    bank?: string | null,
    accountId?: string | null,
    since?: string | null,
    before?: string | null,
    includePending = false,
    limit = DEFAULT_PAGE_SIZE,
    offset = 0,
  ): Promise<Row> {
    const { data, window } = await this.transactions(bank, accountId, since, before, includePending, limit, offset);
    return paged(data, offset, limit, window);
  }

  async listPending(bank?: string | null, accountId?: string | null, limit = DEFAULT_PAGE_SIZE, offset = 0): Promise<Row> {
    const { data } = await this.transactions(bank, accountId, null, null, true, limit, offset);
    data.results = data.results.filter((t) => t.pending === true);
    return paged(data, offset, limit);
  }

  private async transactions(
    bank: string | null | undefined,
    accountId: string | null | undefined,
    since: string | null | undefined,
    before: string | null | undefined,
    includePending: boolean,
    limit: number,
    offset: number,
  ) {
    if (!Number.isInteger(limit) || limit < 1 || limit > MAX_PAGE_SIZE) {
      throw new InvalidRequestError(`'limit' must be between 1 and ${MAX_PAGE_SIZE}.`);
    }
    if (!Number.isInteger(offset) || offset < 0) throw new InvalidRequestError("'offset' must be 0 or more.");
    const [start, end] = resolveWindow(since, before, this.maxRangeDays);
    const data = await this.forEach(bank, async (_, p) => {
      const ids = accountId ? [accountId] : (await p.listAccounts()).map((a) => a.account_id);
      const out: Row[] = [];
      for (const id of ids) {
        const page = await p.listTransactions(id, start, end, includePending);
        out.push(...(page.transactions as Transaction[] as unknown as Row[]));
      }
      return out;
    });
    return { data, window: { since: start, before: end } };
  }
}

const cmp = (a: unknown, b: unknown) => (String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0);

function paged(
  data: { banks_queried: string[]; results: Row[]; errors: Row[] },
  offset: number,
  limit: number,
  window?: { since: string; before: string },
): Row {
  const rows = [...data.results].sort((a, b) => cmp(a.date, b.date) || cmp(a.bank, b.bank) || cmp(a.id, b.id));
  const out: Row = {
    ...data,
    results: rows.slice(offset, offset + limit),
    total: rows.length,
    offset,
    limit,
    next_offset: offset + limit < rows.length ? offset + limit : null,
  };
  if (window) out.window = window;
  return out;
}
