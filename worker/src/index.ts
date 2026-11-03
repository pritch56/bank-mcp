/**
 * Cloudflare Worker entrypoint: bank-mcp over MCP Streamable HTTP at /mcp (stateless).
 * Every request must carry a valid Cloudflare Access token; see worker/README.md.
 */

import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { verifyAccess, type AccessEnv } from "./access.js";
import { buildServer } from "./server.js";
import { BankService, type ServiceEnv } from "./service.js";
import { BankTokenStore } from "./store.js";

export interface Env extends AccessEnv, ServiceEnv {
  TOKENS: KVNamespace;
}

const VERSION = "0.1.2";

function text(status: number, body: string, extra: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", ...extra } });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const access = await verifyAccess(request, env);
    if (!access.ok) return text(access.status, access.reason);

    const { pathname } = new URL(request.url);
    if (pathname !== "/mcp") return text(404, "Not found. The MCP endpoint is /mcp.");

    const service = new BankService(new BankTokenStore(env.TOKENS), env);
    const server = buildServer(service, VERSION);
    // Stateless: a fresh server and transport per request, plain JSON responses (no SSE session).
    const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    await server.connect(transport);
    try {
      const response = await transport.handleRequest(request);
      const headers = new Headers(response.headers);
      headers.set("Cache-Control", "no-store");
      return new Response(response.body, { status: response.status, headers });
    } catch (e) {
      console.error("request failed:", (e as Error)?.name);
      return text(500, "Internal error.");
    }
  },
} satisfies ExportedHandler<Env>;
