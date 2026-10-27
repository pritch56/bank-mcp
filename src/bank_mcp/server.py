"""The MCP server: five read-only tools over every linked bank.

    list_connections   linked banks and when each consent lapses (no network call)
    list_accounts      accounts and cards
    get_balance        balances
    list_transactions  settled transactions in a date range, paged
    list_pending       pending transactions

Every tool except `list_connections` takes an optional `bank` nickname; leave it out to query
all linked banks. With one bank named, a lapsed or revoked consent is raised as an error that
names the bank. With all banks, the banks that still work are returned and the ones that need
re-linking are listed under `errors`.
"""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .bank_store import BankTokenStore, normalise_nickname
from .errors import (
    BankMcpError,
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    NotConnectedError,
    TokenExpiredError,
)
from .models import resolve_window
from .providers.base import Provider
from .providers.truelayer import TrueLayerConfig, TrueLayerProvider

DEFAULT_MAX_RANGE_DAYS = 366
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
# Lapsed consent, revoked consent, or a token that can no longer be refreshed: only re-linking helps.
RECONNECT_ERRORS = (ConsentExpiredError, TokenExpiredError, NotConnectedError)

ProviderFactory = Callable[[str], Provider]


def reconnect_message(nickname: str) -> str:
    return (
        f"Reconnect required for '{nickname}': its consent has expired or was revoked. "
        f"Run `bank-mcp-link --provider <provider-id> --name {nickname}` on your own machine."
    )


class ReconnectRequiredError(BankMcpError):
    """A bank's consent has lapsed or been revoked. The message names the bank."""

    code = "reconnect_required"

    def __init__(self, nickname: str):
        super().__init__(reconnect_message(nickname))
        self.nickname = nickname


def truelayer_factory(banks: BankTokenStore, env: dict[str, str] | None = None) -> ProviderFactory:
    """Build a TrueLayer provider for one linked bank. The provider id and environment recorded
    at link time win over the process environment."""

    def build(nickname: str) -> Provider:
        tokens = banks.load(nickname) or {}
        settings = dict(os.environ if env is None else env)
        if tokens.get("provider_id"):
            settings["TRUELAYER_PROVIDER_ID"] = str(tokens["provider_id"])
        if tokens.get("environment"):
            settings["TRUELAYER_ENV"] = str(tokens["environment"])
        return TrueLayerProvider(TrueLayerConfig.from_env(settings), banks.for_bank(nickname))

    return build


class BankService:
    """The logic behind the tools, free of MCP so it can be tested directly."""

    def __init__(
        self,
        banks: BankTokenStore | None = None,
        provider_factory: ProviderFactory | None = None,
        max_range_days: int | None = None,
    ):
        self.banks = banks or BankTokenStore()
        self._factory = provider_factory or truelayer_factory(self.banks)
        self.max_range_days = max_range_days or _env_int("BANK_MAX_RANGE_DAYS", DEFAULT_MAX_RANGE_DAYS)

    # ------------------------------------------------------------ plumbing

    def _targets(self, bank: str | None) -> list[str]:
        if bank is not None and str(bank).strip():
            name = normalise_nickname(bank)
            if name not in self.banks.nicknames():
                raise InvalidRequestError(
                    f"No bank is linked as '{name}'. Linked banks: {', '.join(self.banks.nicknames()) or 'none'}."
                )
            return [name]
        names = self.banks.nicknames()
        if not names:
            raise NotConnectedError("No banks are linked yet. Run `bank-mcp-link --provider <id> --name <nickname>`.")
        return names

    async def _for_each(
        self, bank: str | None, work: Callable[[str, Provider], Awaitable[list[dict[str, Any]]]]
    ) -> dict[str, Any]:
        """Run `work` against each targeted bank; rows come back tagged with their bank."""
        targets = self._targets(bank)
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for nickname in targets:
            try:
                if self.banks.is_expired(nickname):
                    raise ReconnectRequiredError(nickname)
                provider = self._factory(nickname)
                try:
                    got = await work(nickname, provider)
                finally:
                    await provider.aclose()
                rows.extend({"bank": nickname, **row} for row in got)
            except (*RECONNECT_ERRORS, ReconnectRequiredError) as exc:
                failure = exc if isinstance(exc, ReconnectRequiredError) else ReconnectRequiredError(nickname)
                if bank is not None:
                    raise failure from None
                errors.append({"bank": nickname, "error": failure.code, "message": failure.message})
            except BankMcpError as exc:
                if bank is not None:
                    raise
                errors.append({"bank": nickname, "error": exc.code, "message": exc.message})
        return {"banks_queried": targets, "results": rows, "errors": errors}

    # --------------------------------------------------------------- tools

    def list_connections(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        connections = []
        for nickname in self.banks.nicknames():
            stamp = self.banks.expires_at(nickname)
            expired = self.banks.is_expired(nickname, now)
            days = None
            if stamp:
                days = (datetime.fromisoformat(stamp.replace("Z", "+00:00")) - now).days
            entry: dict[str, Any] = {
                "bank": nickname,
                "status": "reconnect_required" if expired else "linked",
                "consent_expires_at": stamp,
                "days_remaining": days,
            }
            if expired:
                entry["message"] = reconnect_message(nickname)
            connections.append(entry)
        return {"connections": connections}

    async def list_accounts(self, bank: str | None = None) -> dict[str, Any]:
        async def work(_: str, provider: Provider) -> list[dict[str, Any]]:
            return [a.to_dict() for a in await provider.list_accounts()]

        return await self._for_each(bank, work)

    async def get_balance(self, bank: str | None = None, account_id: str | None = None) -> dict[str, Any]:
        async def work(_: str, provider: Provider) -> list[dict[str, Any]]:
            ids = [account_id] if account_id else [a.account_id for a in await provider.list_accounts()]
            return [(await provider.get_balance(i)).to_dict() for i in ids]

        return await self._for_each(bank, work)

    async def list_transactions(
        self,
        bank: str | None = None,
        account_id: str | None = None,
        since: str | None = None,
        before: str | None = None,
        include_pending: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Transactions with since <= date < before (default: the last 30 days), oldest first,
        `limit` per page starting at `offset`. `next_offset` is null on the last page."""
        data, window = await self._transactions(bank, account_id, since, before, include_pending, limit, offset)
        return _paged(data, offset, limit, window=window)

    async def _transactions(
        self,
        bank: str | None,
        account_id: str | None,
        since: str | None,
        before: str | None,
        include_pending: bool,
        limit: int,
        offset: int,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise InvalidRequestError(f"'limit' must be between 1 and {MAX_PAGE_SIZE}.")
        if offset < 0:
            raise InvalidRequestError("'offset' must be 0 or more.")
        start, end = resolve_window(since, before, self.max_range_days)

        async def work(_: str, provider: Provider) -> list[dict[str, Any]]:
            ids = [account_id] if account_id else [a.account_id for a in await provider.list_accounts()]
            out: list[dict[str, Any]] = []
            for i in ids:
                page = await provider.list_transactions(i, start, end, include_pending=include_pending)
                out.extend(t.to_dict() for t in page.transactions)
            return out

        data = await self._for_each(bank, work)
        return data, {"since": start.isoformat(), "before": end.isoformat()}

    async def list_pending(
        self,
        bank: str | None = None,
        account_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Pending (not yet settled) transactions from the last 30 days."""
        data, _ = await self._transactions(bank, account_id, None, None, True, limit, offset)
        data["results"] = [t for t in data["results"] if t["pending"]]
        return _paged(data, offset, limit)


def _paged(data: dict[str, Any], offset: int, limit: int, window: dict[str, str] | None = None) -> dict[str, Any]:
    rows = sorted(data["results"], key=lambda t: (t["date"], t["bank"], t["id"]))
    page = rows[offset : offset + limit]
    out = {
        **data,
        "results": page,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(rows) else None,
    }
    if window:
        out["window"] = window
    return out


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        raise ConfigError(f"{name} must be an integer.") from None


READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def _tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Report our own errors to the client as tool errors; the SDK hides the text of anything else."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except BankMcpError as exc:
            raise ToolError(str(exc)) from None

    return wrapper


def build_server(service: BankService | None = None) -> MCPServer:
    """Register the five read-only tools."""
    svc = service or BankService()
    server = MCPServer("bank-mcp", instructions="Read-only access to your linked UK banks and cards.")

    @server.tool(annotations=READ_ONLY, structured_output=True)
    @_tool_errors
    def list_connections() -> dict[str, Any]:
        """List linked banks with consent expiry. Banks needing re-linking say 'reconnect_required'."""
        return svc.list_connections()

    @server.tool(annotations=READ_ONLY, structured_output=True)
    @_tool_errors
    async def list_accounts(bank: str | None = None) -> dict[str, Any]:
        """List accounts and cards. `bank` is a nickname; omit it to query all linked banks."""
        return await svc.list_accounts(bank)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    @_tool_errors
    async def get_balance(bank: str | None = None, account_id: str | None = None) -> dict[str, Any]:
        """Balances. `bank` is a nickname (omit for all banks); `account_id` narrows to one account."""
        return await svc.get_balance(bank, account_id)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    @_tool_errors
    async def list_transactions(
        bank: str | None = None,
        account_id: str | None = None,
        since: str | None = None,
        before: str | None = None,
        include_pending: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Transactions with since <= date < before (ISO dates; default the last 30 days), oldest first.
        Page with `limit` and `offset`; `next_offset` is null on the last page."""
        return await svc.list_transactions(bank, account_id, since, before, include_pending, limit, offset)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    @_tool_errors
    async def list_pending(
        bank: str | None = None, account_id: str | None = None, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        """Pending (not yet settled) transactions. `bank` is a nickname; omit it for all banks."""
        return await svc.list_pending(bank, account_id, limit, offset)

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
