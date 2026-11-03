/** Error types, mirroring bank_mcp.errors. Messages never contain secrets. */

export class BankMcpError extends Error {
  static code = "error";
  readonly message_: string;

  constructor(message: string, code?: string) {
    const c = code ?? (new.target as typeof BankMcpError).code;
    super(`[${c}] ${message}`);
    this.name = new.target.name;
    this.code = c;
    this.message_ = message;
  }

  readonly code: string;

  /** The message without the "[code] " prefix. */
  get detail(): string {
    return this.message_;
  }
}

export class ConfigError extends BankMcpError {
  static override code = "config_error";
}
export class NotConnectedError extends BankMcpError {
  static override code = "not_connected";
}
export class ConsentExpiredError extends BankMcpError {
  static override code = "consent_expired";
}
export class TokenExpiredError extends BankMcpError {
  static override code = "token_expired";
}
export class RateLimitError extends BankMcpError {
  static override code = "rate_limited";
  constructor(
    message: string,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
  }
}
export class ProviderError extends BankMcpError {
  static override code = "provider_error";
}
export class NotSupportedError extends ProviderError {
  static override code = "provider_error";
}
export class TokenStoreError extends BankMcpError {
  static override code = "token_store_error";
}
export class AccountNotFoundError extends BankMcpError {
  static override code = "account_not_found";
}
export class InvalidRequestError extends BankMcpError {
  static override code = "invalid_request";
}
export class ScaRequiredError extends BankMcpError {
  static override code = "sca_required";
}
export class ReconnectRequiredError extends BankMcpError {
  static override code = "reconnect_required";
  constructor(readonly nickname: string) {
    super(reconnectMessage(nickname));
  }
}

export function reconnectMessage(nickname: string): string {
  return (
    `Reconnect required for '${nickname}': its consent has expired or was revoked. ` +
    `Run \`bank-mcp-link --provider <provider-id> --name ${nickname}\` on your own machine, ` +
    "then run `npm run seed` in worker/ to upload the new tokens."
  );
}
