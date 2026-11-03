import { describe, expect, it } from "vitest";
import { addDays, londonDate, mergeTransactions, makeTransaction, resolveWindow, stableId, toMinor, utcZ } from "../src/models.js";
import { windows } from "../src/truelayer.js";

describe("toMinor", () => {
  it("handles floats without drift and rounds half away from zero", () => {
    expect(toMinor(24.25)).toBe(2425);
    expect(toMinor(-24.25)).toBe(-2425);
    expect(toMinor("0.005")).toBe(1);
    expect(toMinor("-0.005")).toBe(-1);
    expect(toMinor(1e-7)).toBe(0);
    expect(toMinor(100, "JPY")).toBe(100);
    expect(toMinor("1.234", "KWD")).toBe(1234);
  });
  it("rejects garbage", () => {
    expect(() => toMinor("abc")).toThrow(RangeError);
    expect(() => toMinor(Number.NaN)).toThrow(RangeError);
    expect(() => toMinor(1e13)).toThrow(RangeError);
  });
});

describe("dates", () => {
  it("converts offset timestamps to London dates", () => {
    expect(londonDate("2026-06-30T23:30:00Z")).toBe("2026-07-01"); // BST
    expect(londonDate("2026-01-15T23:30:00Z")).toBe("2026-01-15"); // GMT
    expect(londonDate("2026-03-01T10:00:00")).toBe("2026-03-01"); // no offset: face value
    expect(londonDate("2026-03-01")).toBe("2026-03-01");
    expect(() => londonDate("nonsense")).toThrow();
  });
  it("normalises to UTC Z", () => {
    expect(utcZ("2026-06-30T23:30:00+01:00")).toBe("2026-06-30T22:30:00Z");
    expect(utcZ("2026-06-30T23:30:00.123456Z")).toBe("2026-06-30T23:30:00Z");
    expect(utcZ("2026-06-30T23:30:00")).toBe("2026-06-30T23:30:00Z");
    expect(utcZ("x")).toBeNull();
  });
  it("resolves windows", () => {
    const now = new Date("2026-10-10T12:00:00Z");
    expect(resolveWindow(null, null, 366, now)).toEqual(["2026-09-11", "2026-10-11"]);
    expect(() => resolveWindow("2026-10-05", "2026-10-01", 366, now)).toThrow();
    expect(() => resolveWindow("2024-01-01", "2026-01-01", 366, now)).toThrow(/maximum/);
  });
  it("walks long ranges in overlapping windows", () => {
    expect(windows("2026-01-01", "2026-01-05", 88)).toEqual([["2026-01-01", "2026-01-05"]]);
    const w = windows("2026-01-01", "2026-06-01", 88);
    expect(w[0]![1]).toBe(w[1]![0]);
    expect(w.at(-1)![1]).toBe("2026-06-01");
    expect(addDays("2026-02-28", 1)).toBe("2026-03-01");
  });
});

describe("ids", () => {
  it("matches the Python implementation", () => {
    // python: stable_id("tl", "abc") -> sha256("tl\x1fabc")[:24]
    expect(stableId("tl", "abc")).toBe("bank_6629fbd65afe6b18aae0a4b3");
  });
  it("keeps two different transactions that share an id", () => {
    const t = (d: string) =>
      makeTransaction({ id: "x", account_id: "a", date: d, amount_minor: -100, currency: "GBP", description: "d", merchant: null, category: null, pending: false, notes: null });
    const m = mergeTransactions([t("2026-01-01"), t("2026-01-01"), t("2026-01-02")]);
    expect(m.size).toBe(2);
  });
});
