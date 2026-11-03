/**
 * Per-bank token storage in Workers KV.
 *
 * The document is the same JSON that `bank-mcp-link` writes to ~/.bank-mcp/tokens.json
 *   { "banks": { "<nickname>": { "tokens": {...}, "expires_at": "...Z" | null, "updated_at": "..." } } }
 * stored under one KV key, so `npm run seed` can upload it unchanged. KV is eventually
 * consistent; that is acceptable for one user, but a rotated refresh token may take up to a
 * minute to be visible at other locations.
 */

import { InvalidRequestError, TokenStoreError } from "./errors.js";
import { utcZ } from "./models.js";

export const KV_KEY = "tokens";
const NICKNAME_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

export type TokenDoc = Record<string, unknown>;

export interface TokenStore {
  load(): Promise<TokenDoc | null>;
  save(tokens: TokenDoc): Promise<void>;
}

export function normaliseNickname(nickname: string): string {
  const name = String(nickname ?? "").trim().toLowerCase();
  if (!NICKNAME_RE.test(name)) {
    throw new InvalidRequestError(
      "A bank nickname must be 1-32 characters of letters, digits, '-' or '_' and start with a letter or digit.",
    );
  }
  return name;
}

interface BankEntry {
  tokens?: TokenDoc;
  expires_at?: string | null;
  updated_at?: string;
}
type Banks = Record<string, BankEntry>;

export class BankTokenStore {
  private cache: Banks | null = null;

  constructor(private readonly kv: KVNamespace) {}

  private async banks(): Promise<Banks> {
    if (this.cache) return this.cache;
    let doc: unknown;
    try {
      doc = await this.kv.get(KV_KEY, "json");
    } catch (e) {
      throw new TokenStoreError(`The token store could not be read (${(e as Error).name}). Check the KV binding and the seeded 'tokens' key.`);
    }
    const banks = (doc as { banks?: unknown } | null)?.banks;
    this.cache = banks && typeof banks === "object" ? (banks as Banks) : {};
    return this.cache;
  }

  async nicknames(): Promise<string[]> {
    return Object.keys(await this.banks()).sort();
  }

  async load(nickname: string): Promise<TokenDoc | null> {
    const tokens = (await this.banks())[normaliseNickname(nickname)]?.tokens;
    return tokens && typeof tokens === "object" ? { ...tokens } : null;
  }

  async expiresAt(nickname: string): Promise<string | null> {
    const stamp = (await this.banks())[normaliseNickname(nickname)]?.expires_at;
    return typeof stamp === "string" && stamp ? stamp : null;
  }

  async isExpired(nickname: string, now: Date = new Date()): Promise<boolean> {
    const stamp = await this.expiresAt(nickname);
    if (!stamp) return false;
    const at = Date.parse(stamp);
    return Number.isNaN(at) ? false : at <= now.getTime();
  }

  /** Store (or replace) one bank's tokens, keeping its recorded consent expiry. */
  async save(nickname: string, tokens: TokenDoc): Promise<void> {
    const name = normaliseNickname(nickname);
    // Re-read so a concurrent refresh of another bank is not overwritten with stale data.
    this.cache = null;
    const banks = { ...(await this.banks()) };
    const previous = banks[name];
    banks[name] = {
      tokens: { ...tokens },
      expires_at: utcZ(previous?.expires_at) ?? null,
      updated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    };
    try {
      await this.kv.put(KV_KEY, JSON.stringify({ banks }));
    } catch (e) {
      throw new TokenStoreError(`Could not write the token store (${(e as Error).name}).`);
    }
    this.cache = banks;
  }

  forBank(nickname: string): TokenStore {
    const name = normaliseNickname(nickname);
    return {
      load: () => this.load(name),
      save: (tokens) => this.save(name, tokens),
    };
  }
}
