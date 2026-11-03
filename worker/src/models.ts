/**
 * Normalised data shapes shared by every provider (port of bank_mcp.models).
 *
 * amount_minor is a signed integer in minor units (pence): NEGATIVE = money out,
 * POSITIVE = money in. Dates are calendar dates in Europe/London.
 */

import { createHash } from "node:crypto";
import { InvalidRequestError } from "./errors.js";

export const SOURCE = "bank";

const NON_DEFAULT_EXPONENTS: Record<string, number> = {
  BIF: 0, CLP: 0, DJF: 0, GNF: 0, ISK: 0, JPY: 0, KMF: 0, KRW: 0, PYG: 0, RWF: 0, UGX: 0, UYI: 0, VND: 0,
  VUV: 0, XAF: 0, XOF: 0, XPF: 0,
  BHD: 3, IQD: 3, JOD: 3, KWD: 3, LYD: 3, OMR: 3, TND: 3,
  CLF: 4, UYW: 4,
};

const MAX_ABS_AMOUNT = 10 ** 12;
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g;

export interface Transaction {
  id: string;
  source: string;
  account_id: string;
  date: string;
  amount_minor: number;
  currency: string;
  description: string;
  merchant: string | null;
  category: string | null;
  pending: boolean;
  notes: string | null;
}

export interface TransactionPage {
  transactions: Transaction[];
  pendingExcluded: number;
  earliestPendingDate: string | null;
  pendingSupported: boolean;
}

export interface Account {
  id: string;
  account_id: string;
  name: string;
  currency: string;
  type: string;
  card_network: string | null;
  last4: string | null;
  provider: string;
  source: string;
}

export interface Balance {
  account_id: string;
  currency: string;
  balance_minor: number | null;
  available_credit_minor: number | null;
  credit_limit_minor: number | null;
  last_statement_balance_minor: number | null;
  last_statement_date: string | null;
  payment_due_minor: number | null;
  payment_due_date: string | null;
  as_of: string | null;
  notes: string | null;
  source: string;
}

export function currencyExponent(currency: string): number {
  return NON_DEFAULT_EXPONENTS[(currency || "").toUpperCase()] ?? 2;
}

/** Convert a decimal amount to integer minor units, rounding half away from zero. Throws RangeError if unusable. */
export function toMinor(amount: unknown, currency = "GBP"): number {
  const text = typeof amount === "number" ? numberToPlain(amount) : String(amount ?? "").replace(/,/g, "").trim();
  const m = /^([+-])?(\d*)(?:\.(\d*))?$/.exec(text);
  if (!m || (!m[2] && !m[3])) throw new RangeError(`not a usable number: ${JSON.stringify(String(amount).slice(0, 40))}`);
  const exp = currencyExponent(currency);
  const frac = m[3] ?? "";
  const digits = (m[2] || "0") + frac.padEnd(exp, "0").slice(0, exp);
  let units = BigInt(digits);
  if (frac.length > exp && frac.charCodeAt(exp) >= 53 /* '5' */) units += 1n;
  const value = Number(units);
  if (Math.abs(Number(m[2] || "0")) > MAX_ABS_AMOUNT || !Number.isSafeInteger(value)) {
    throw new RangeError(`not a usable number: ${JSON.stringify(String(amount).slice(0, 40))}`);
  }
  return m[1] === "-" && value !== 0 ? -value : value;
}

function numberToPlain(n: number): string {
  if (!Number.isFinite(n)) return "NaN";
  const s = String(n);
  if (!/e/i.test(s)) return s;
  return n.toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
}

export function cleanText(value: unknown): string {
  return String(value ?? "")
    .replace(CONTROL_RE, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** 'bank_' + first 24 hex chars of SHA-256 over the parts joined by \x1f (same ids as the Python server). */
export function stableId(...parts: unknown[]): string {
  const joined = parts.map((p) => (p === null || p === undefined ? "" : String(p))).join("\x1f");
  return `${SOURCE}_${createHash("sha256").update(joined, "utf8").digest("hex").slice(0, 24)}`;
}

const londonFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/London",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function londonDay(d: Date): string {
  const p = Object.fromEntries(londonFmt.formatToParts(d).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

export function londonToday(now: Date = new Date()): string {
  return londonDay(now);
}

export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const DATETIME = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2})(?:[.,](\d+))?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

function validDate(s: string): string {
  const d = new Date(`${s}T00:00:00Z`);
  if (Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== s) throw new RangeError(`invalid date: ${s}`);
  return s;
}

/** Parse an ISO 8601 timestamp. Returns the instant (when it has an offset) or just the face-value date. */
function parseTimestamp(text: string): { date: string; instant: Date | null } {
  const t = text.trim();
  if (DATE_ONLY.test(t)) return { date: validDate(t), instant: null };
  const m = DATETIME.exec(t);
  if (!m) throw new RangeError(`not a date/time: ${JSON.stringify(t.slice(0, 40))}`);
  const day = validDate(m[1]!);
  if (!m[5]) return { date: day, instant: null };
  let offset = m[5].toUpperCase();
  if (offset !== "Z") {
    const digits = offset.replace(":", "");
    offset = `${digits.slice(0, 3)}:${digits.slice(3, 5) || "00"}`;
  }
  const ms = (m[4] ?? "").slice(0, 3).padEnd(3, "0");
  const instant = new Date(`${day}T${m[2]}:${m[3] ?? "00"}.${ms}${offset}`);
  if (Number.isNaN(instant.getTime())) throw new RangeError(`not a date/time: ${JSON.stringify(t.slice(0, 40))}`);
  return { date: day, instant };
}

/** Calendar date (YYYY-MM-DD) in Europe/London for a provider timestamp. Throws RangeError if unparseable. */
export function londonDate(value: unknown): string {
  const { date, instant } = parseTimestamp(String(value ?? ""));
  return instant ? londonDay(instant) : date;
}

/** Normalise a provider timestamp to ISO 8601 UTC ending in 'Z' (null if unparseable). Offset-less times are taken as UTC. */
export function utcZ(value: unknown): string | null {
  try {
    const text = String(value ?? "").trim();
    const { date, instant } = parseTimestamp(text);
    let at = instant;
    if (!at) {
      const m = DATETIME.exec(text);
      at = new Date(m ? `${date}T${m[2]}:${m[3] ?? "00"}Z` : `${date}T00:00:00Z`);
    }
    return at.toISOString().replace(/\.\d{3}Z$/, "Z");
  } catch {
    return null;
  }
}

export function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

export function parseBound(value: string | null | undefined, name: string): string | null {
  if (value === null || value === undefined || String(value).trim() === "") return null;
  try {
    return londonDate(String(value).trim());
  } catch {
    throw new InvalidRequestError(
      `'${name}' must be an ISO 8601 date or datetime (e.g. 2026-10-03 or 2026-10-03T00:00:00Z); got ${JSON.stringify(
        cleanText(value).slice(0, 40),
      )}.`,
    );
  }
}

/** Apply the defaults and limits of list_transactions: since <= date < before. */
export function resolveWindow(
  since: string | null | undefined,
  before: string | null | undefined,
  maxRangeDays: number,
  now: Date = new Date(),
): [string, string] {
  const beforeD = parseBound(before, "before") ?? addDays(londonToday(now), 1);
  const sinceD = parseBound(since, "since") ?? addDays(beforeD, -30);
  if (sinceD >= beforeD) throw new InvalidRequestError("'since' must be earlier than 'before'.");
  const span = daysBetween(sinceD, beforeD);
  if (span > maxRangeDays) {
    throw new InvalidRequestError(
      `The requested range is ${span} days; the maximum is ${maxRangeDays} (BANK_MAX_RANGE_DAYS). Ask for a shorter range.`,
    );
  }
  return [sinceD, beforeD];
}

/** since is inclusive, before is exclusive. */
export function dateInWindow(isoDate: string, since: string | null, before: string | null): boolean {
  const d = isoDate.slice(0, 10);
  if (since !== null && d < since) return false;
  return !(before !== null && d >= before);
}

function contentKey(t: Transaction): string {
  return JSON.stringify([t.date, t.amount_minor, t.currency, t.description]);
}

/** Merge rows by id without losing data (see bank_mcp.models.merge_transactions). */
export function mergeTransactions(
  rows: Transaction[],
  into: Map<string, Transaction> = new Map(),
): Map<string, Transaction> {
  for (const t of rows) {
    const existing = into.get(t.id);
    if (!existing) {
      into.set(t.id, t);
      continue;
    }
    if (contentKey(existing) === contentKey(t) && existing.pending === t.pending) continue;
    let seq = 0;
    let newId: string;
    let clash: Transaction | undefined;
    for (;;) {
      newId = stableId("dup", t.id, t.date, t.amount_minor, t.currency, t.description, seq);
      clash = into.get(newId);
      if (!clash || contentKey(clash) === contentKey(t)) break;
      seq += 1;
    }
    if (!clash) into.set(newId, { ...t, id: newId });
  }
  return into;
}

export function makeTransaction(t: Omit<Transaction, "source">): Transaction {
  return {
    id: t.id,
    source: SOURCE,
    account_id: t.account_id,
    date: t.date,
    amount_minor: t.amount_minor,
    currency: t.currency,
    description: t.description,
    merchant: t.merchant,
    category: t.category,
    pending: t.pending,
    notes: t.notes || null,
  };
}
