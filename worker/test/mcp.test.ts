import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { describe, expect, it } from "vitest";
import { buildServer } from "../src/server.js";
import { BankService } from "../src/service.js";
import { BankTokenStore, KV_KEY } from "../src/store.js";

const kv = {
  async get() {
    return JSON.parse('{"banks":{"lapsed":{"tokens":{"refresh_token":"r-lapsed-token"},"expires_at":"2020-01-01T00:00:00Z"}}}');
  },
  async put() {},
} as unknown as KVNamespace;
void KV_KEY;

async function rpc(method: string, params: unknown, id = 1) {
  const server = buildServer(new BankService(new BankTokenStore(kv), {}), "test");
  const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  await server.connect(transport);
  const res = await transport.handleRequest(
    new Request("https://x/mcp", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
    }),
  );
  return res.json() as Promise<{ result: any }>;
}

describe("MCP over streamable HTTP (stateless)", () => {
  it("lists the five read-only tools", async () => {
    const { result } = await rpc("tools/list", {});
    expect(result.tools.map((t: { name: string }) => t.name).sort()).toEqual(
      ["get_balance", "list_accounts", "list_connections", "list_pending", "list_transactions"],
    );
    expect(result.tools.every((t: { annotations: { readOnlyHint: boolean } }) => t.annotations.readOnlyHint)).toBe(true);
  });
  it("calls a tool and returns structured content", async () => {
    const { result } = await rpc("tools/call", { name: "list_connections", arguments: {} });
    expect(result.structuredContent.connections[0]).toMatchObject({ bank: "lapsed", status: "reconnect_required" });
  });
  it("reports our errors as tool errors with text", async () => {
    const { result } = await rpc("tools/call", { name: "list_accounts", arguments: { bank: "lapsed" } });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toMatch(/Reconnect required for 'lapsed'/);
  });
});
