/**
 * Cloudflare Access authentication.
 *
 * The Worker sits behind an Access application. Access injects a signed JWT in the
 * `Cf-Access-Jwt-Assertion` header; we verify its signature, issuer and audience here as well, so
 * a misconfigured route (or the workers.dev URL) can never serve bank data unauthenticated.
 * If Access is not configured the Worker fails closed.
 */

import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";

export interface AccessEnv {
  /** Zero Trust team name: the "<team>" in <team>.cloudflareaccess.com */
  ACCESS_TEAM_DOMAIN?: string;
  /** Application Audience (AUD) tag of the Access application. */
  ACCESS_AUD?: string;
}

const jwksCache = new Map<string, JWTVerifyGetKey>();

export type AccessResult = { ok: true; subject: string } | { ok: false; status: 401 | 403 | 503; reason: string };

function teamOrigin(env: AccessEnv): string | null {
  const raw = (env.ACCESS_TEAM_DOMAIN ?? "").trim().replace(/^https?:\/\//, "").replace(/\/+$/, "");
  if (!raw) return null;
  const host = raw.endsWith(".cloudflareaccess.com") ? raw : `${raw}.cloudflareaccess.com`;
  return /^[a-z0-9-]+\.cloudflareaccess\.com$/i.test(host) ? `https://${host.toLowerCase()}` : null;
}

export async function verifyAccess(request: Request, env: AccessEnv, jwks?: JWTVerifyGetKey): Promise<AccessResult> {
  const origin = teamOrigin(env);
  const aud = (env.ACCESS_AUD ?? "").trim();
  if (!origin || !aud) {
    return { ok: false, status: 503, reason: "Cloudflare Access is not configured (ACCESS_TEAM_DOMAIN / ACCESS_AUD)." };
  }
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) return { ok: false, status: 401, reason: "Missing Cloudflare Access token." };
  let keys = jwks ?? jwksCache.get(origin);
  if (!keys) {
    keys = createRemoteJWKSet(new URL(`${origin}/cdn-cgi/access/certs`));
    jwksCache.set(origin, keys);
  }
  try {
    const { payload } = await jwtVerify(token, keys, { issuer: origin, audience: aud, algorithms: ["RS256"] });
    return { ok: true, subject: String(payload.email ?? payload.common_name ?? payload.sub ?? "unknown") };
  } catch {
    return { ok: false, status: 403, reason: "Invalid Cloudflare Access token." };
  }
}
