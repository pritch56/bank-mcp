/** Registers the five read-only tools on an MCP server. */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { BankMcpError } from "./errors.js";
import { DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, type BankService } from "./service.js";

const READ_ONLY = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true } as const;

async function run(fn: () => Promise<Record<string, unknown>> | Record<string, unknown>) {
  try {
    const out = await fn();
    return { content: [{ type: "text" as const, text: JSON.stringify(out) }], structuredContent: out };
  } catch (e) {
    // Only our own errors carry text that is safe to show; anything else is hidden.
    const text = e instanceof BankMcpError ? e.message : "Internal error.";
    if (!(e instanceof BankMcpError)) console.error("tool failed:", (e as Error)?.name);
    return { content: [{ type: "text" as const, text }], isError: true };
  }
}

export function buildServer(svc: BankService, version: string): McpServer {
  const server = new McpServer({ name: "bank-mcp", version }, { instructions: "Read-only access to your linked UK banks and cards." });
  const bank = z.string().optional().describe("Bank nickname; omit to query all linked banks.");
  const accountId = z.string().optional().describe("Narrow to one account id from list_accounts.");
  const limit = z.number().int().min(1).max(MAX_PAGE_SIZE).default(DEFAULT_PAGE_SIZE);
  const offset = z.number().int().min(0).default(0);

  server.registerTool(
    "list_connections",
    { description: "List linked banks with consent expiry. Banks needing re-linking say 'reconnect_required'.", inputSchema: {}, annotations: READ_ONLY },
    () => run(() => svc.listConnections()),
  );
  server.registerTool(
    "list_accounts",
    { description: "List accounts and cards. `bank` is a nickname; omit it to query all linked banks.", inputSchema: { bank }, annotations: READ_ONLY },
    ({ bank }) => run(() => svc.listAccounts(bank)),
  );
  server.registerTool(
    "get_balance",
    { description: "Balances. `bank` is a nickname (omit for all banks); `account_id` narrows to one account.", inputSchema: { bank, account_id: accountId }, annotations: READ_ONLY },
    ({ bank, account_id }) => run(() => svc.getBalance(bank, account_id)),
  );
  server.registerTool(
    "list_transactions",
    {
      description:
        "Transactions with since <= date < before (ISO dates; default the last 30 days), oldest first. Page with `limit` and `offset`; `next_offset` is null on the last page.",
      inputSchema: {
        bank,
        account_id: accountId,
        since: z.string().optional().describe("Inclusive start, ISO 8601 date or datetime."),
        before: z.string().optional().describe("Exclusive end, ISO 8601 date or datetime."),
        include_pending: z.boolean().default(false),
        limit,
        offset,
      },
      annotations: READ_ONLY,
    },
    ({ bank, account_id, since, before, include_pending, limit, offset }) =>
      run(() => svc.listTransactions(bank, account_id, since, before, include_pending, limit, offset)),
  );
  server.registerTool(
    "list_pending",
    {
      description: "Pending (not yet settled) transactions from the last 30 days. `bank` is a nickname; omit it for all banks.",
      inputSchema: { bank, account_id: accountId, limit, offset },
      annotations: READ_ONLY,
    },
    ({ bank, account_id, limit, offset }) => run(() => svc.listPending(bank, account_id, limit, offset)),
  );
  return server;
}
