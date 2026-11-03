#!/usr/bin/env node
/**
 * Upload linked banks from the local token file (written by `bank-mcp-link`) into the Worker's KV
 * namespace, merging with what is already there.
 *
 *   npm run seed -- --bank nationwide        upload one bank
 *   npm run seed -- --all                    upload every bank in the file
 *   npm run seed -- --bank monzo --file ./tokens.json --local   (use the local dev KV)
 *
 * Only upload a bank right after (re)linking it. The Worker rotates refresh tokens in KV, so
 * re-uploading an old local copy of a bank you have not just re-linked would replace a newer
 * refresh token with a stale one.
 */
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    bank: { type: "string", multiple: true },
    all: { type: "boolean", default: false },
    file: { type: "string" },
    local: { type: "boolean", default: false },
    binding: { type: "string", default: "TOKENS" },
    key: { type: "string", default: "tokens" },
    help: { type: "boolean", short: "h", default: false },
  },
});

if (values.help || (!values.all && !values.bank?.length)) {
  console.log("usage: npm run seed -- (--bank <nickname> [--bank ...] | --all) [--file <tokens.json>] [--local]");
  process.exit(values.help ? 0 : 2);
}

const file = values.file ?? process.env.BANK_TOKEN_FILE ?? join(homedir(), ".bank-mcp", "tokens.json");
let local;
try {
  local = JSON.parse(readFileSync(file, "utf8"));
} catch (e) {
  console.error(`Could not read ${file}: ${e.code ?? e.name}. Link a bank first with bank-mcp-link.`);
  process.exit(1);
}
const localBanks = local?.banks && typeof local.banks === "object" ? local.banks : {};
const wanted = values.all ? Object.keys(localBanks) : values.bank.map((b) => b.trim().toLowerCase());
const missing = wanted.filter((b) => !localBanks[b]);
if (missing.length || !wanted.length) {
  console.error(`Not in ${file}: ${missing.join(", ") || "(no banks)"}. Linked: ${Object.keys(localBanks).join(", ") || "none"}.`);
  process.exit(1);
}

const where = values.local ? ["--local"] : ["--remote"];
const base = ["wrangler", "kv", "key"];
const target = ["--binding", values.binding, ...where];
const wrangler = (args, opts = {}) => spawnSync("npx", args, { encoding: "utf8", shell: process.platform === "win32", ...opts });

// Current remote document (a missing key is fine).
const got = wrangler([...base, "get", values.key, ...target]);
let remote = { banks: {} };
if (got.status === 0 && got.stdout.trim()) {
  try {
    remote = JSON.parse(got.stdout);
  } catch {
    console.error("The existing 'tokens' value in KV is not valid JSON; refusing to overwrite it.");
    process.exit(1);
  }
}
const merged = { banks: { ...(remote.banks ?? {}) } };
for (const b of wanted) merged.banks[b] = localBanks[b];

const dir = mkdtempSync(join(tmpdir(), "bank-mcp-seed-"));
const tmp = join(dir, "tokens.json");
try {
  writeFileSync(tmp, JSON.stringify(merged), { mode: 0o600 });
  chmodSync(tmp, 0o600);
  const put = wrangler([...base, "put", values.key, "--path", tmp, ...target], { stdio: "inherit" });
  if (put.status !== 0) process.exit(put.status ?? 1);
  console.log(`Uploaded ${wanted.join(", ")} to KV (${values.local ? "local" : "remote"}).`);
} finally {
  rmSync(dir, { recursive: true, force: true });
}
