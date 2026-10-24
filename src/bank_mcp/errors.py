"""Error types. Every message is written to be actionable without a human reading logs.

Messages never contain secrets (tokens, client secrets, card numbers).
"""

from __future__ import annotations


class BankMcpError(Exception):
    """Base class. `code` is a stable machine-readable string."""

    code = "error"

    def __init__(self, message: str):
        super().__init__(f"[{self.code}] {message}")
        self.message = message


class ConfigError(BankMcpError):
    code = "config_error"


class NotConnectedError(BankMcpError):
    """No token has ever been stored - the consent flow has not been run."""

    code = "not_connected"


class ConsentExpiredError(BankMcpError):
    """The cardholder's Open Banking consent has lapsed or been revoked.

    Only a human can fix this: re-run the consent helper (bank-mcp-link).
    """

    code = "consent_expired"


class TokenExpiredError(BankMcpError):
    """The access token was rejected and could not be refreshed automatically."""

    code = "token_expired"


class RateLimitError(BankMcpError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderError(BankMcpError):
    """Upstream (aggregator or bank) failure that is not the caller's fault."""

    code = "provider_error"


class NotSupportedError(ProviderError):
    """The bank does not offer this endpoint through the aggregator (HTTP 501)."""

    code = "provider_error"


class TokenStoreError(BankMcpError):
    """The token store could not be read or written."""

    code = "token_store_error"


class AccountNotFoundError(BankMcpError):
    code = "account_not_found"


class InvalidRequestError(BankMcpError):
    code = "invalid_request"


class ScaRequiredError(BankMcpError):
    """The bank requires a fresh strong-customer-authentication for this request
    (typically: history older than ~90 days requested long after the card was linked)."""

    code = "sca_required"


class MalformedImportError(BankMcpError):
    """A statement file could not be parsed."""

    code = "malformed_import_file"
