"""KAN-24: the five read-only MCP tools, against a mocked provider."""

from datetime import UTC, date, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bank_mcp.bank_store import BankTokenStore
from bank_mcp.errors import ConsentExpiredError, InvalidRequestError, NotConnectedError, ProviderError
from bank_mcp.models import Account, Balance, Transaction, TransactionPage
from bank_mcp.providers.base import Provider
from bank_mcp.server import BankService, ReconnectRequiredError, build_server
from bank_mcp.token_store import FileTokenStore
from tests.conftest import run

TOOLS = {"list_connections", "list_accounts", "get_balance", "list_transactions", "list_pending"}
FUTURE = datetime.now(UTC) + timedelta(days=60)
PAST = datetime.now(UTC) - timedelta(days=1)


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, bank: str, txns: int = 3, fail: Exception | None = None):
        self.bank = bank
        self.fail = fail
        self.closed = False
        self.calls: list[tuple] = []
        self.rows = [
            Transaction(
                id=f"{bank}-{n}",
                account_id=f"{bank}-acc",
                date=f"2026-10-{n + 1:02d}",
                amount_minor=-100 * (n + 1),
                currency="GBP",
                description=f"{bank} purchase {n}",
            )
            for n in range(txns)
        ]
        self.rows.append(
            Transaction(
                id=f"{bank}-p",
                account_id=f"{bank}-acc",
                date="2026-10-20",
                amount_minor=-5,
                currency="GBP",
                description="pending",
                pending=True,
            )
        )

    async def list_accounts(self):
        self.calls.append(("accounts",))
        if self.fail:
            raise self.fail
        return [Account(account_id=f"{self.bank}-acc", name=f"{self.bank} account", currency="GBP", provider="fake")]

    async def get_balance(self, account_id):
        self.calls.append(("balance", account_id))
        return Balance(account_id=account_id, currency="GBP", balance_minor=1234)

    async def list_transactions(self, account_id, since, before, include_pending=False):
        self.calls.append(("transactions", account_id, since, before, include_pending))
        rows = [r for r in self.rows if since <= date.fromisoformat(r.date) < before]
        return TransactionPage(transactions=[r for r in rows if include_pending or not r.pending])

    async def connection_status(self):
        return {"provider": "fake", "connected": True}

    async def aclose(self):
        self.closed = True


@pytest.fixture
def banks(tmp_path):
    store = BankTokenStore(FileTokenStore(tmp_path / "tokens.json"))
    store.save("monzo", {"refresh_token": "r1"}, FUTURE)
    store.save("amex", {"refresh_token": "r2"}, FUTURE)
    return store


@pytest.fixture
def providers():
    return {}


@pytest.fixture
def service(banks, providers):
    def factory(nickname):
        providers[nickname] = FakeProvider(nickname, fail=providers.get("fail", {}).get(nickname))
        return providers[nickname]

    return BankService(banks, factory, max_range_days=366)


def test_five_read_only_tools_are_registered(service):
    tools = run(build_server(service).list_tools())
    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations.read_only_hint is True and t.annotations.destructive_hint is False for t in tools)


def test_bank_and_account_arguments_are_optional(service):
    tools = {t.name: t for t in run(build_server(service).list_tools())}
    for name in TOOLS - {"list_connections"}:
        assert "bank" in tools[name].input_schema["properties"]
        assert "bank" not in tools[name].input_schema.get("required", [])


def test_tools_run_through_the_server(service):
    result = run(build_server(service).call_tool("list_accounts", {"bank": "monzo"}))
    assert result.structured_content["results"][0]["bank"] == "monzo"


def test_list_connections_shows_every_bank_and_flags_lapsed_consent(banks, service):
    banks.save("old", {"refresh_token": "r3"}, PAST)
    by_bank = {c["bank"]: c for c in service.list_connections()["connections"]}
    assert set(by_bank) == {"monzo", "amex", "old"}
    assert by_bank["monzo"]["status"] == "linked" and by_bank["monzo"]["days_remaining"] >= 59
    assert by_bank["old"]["status"] == "reconnect_required"
    assert "'old'" in by_bank["old"]["message"]


def test_list_accounts_single_bank(service, providers):
    data = run(service.list_accounts("Monzo"))
    assert data["banks_queried"] == ["monzo"]
    assert [r["bank"] for r in data["results"]] == ["monzo"]
    assert set(providers) == {"monzo"} and providers["monzo"].closed


def test_list_accounts_all_banks_when_none_given(service):
    data = run(service.list_accounts())
    assert data["banks_queried"] == ["amex", "monzo"]
    assert {r["bank"] for r in data["results"]} == {"amex", "monzo"}
    assert data["errors"] == []


def test_get_balance_single_and_all(service):
    one = run(service.get_balance("amex"))
    assert [(r["bank"], r["balance_minor"]) for r in one["results"]] == [("amex", 1234)]
    both = run(service.get_balance())
    assert {r["bank"] for r in both["results"]} == {"amex", "monzo"}


def test_get_balance_for_one_account(service, providers):
    run(service.get_balance("amex", account_id="amex-acc"))
    assert providers["amex"].calls == [("balance", "amex-acc")]


def test_list_transactions_all_banks_sorted_and_tagged(service):
    data = run(service.list_transactions(since="2026-10-01", before="2026-10-31"))
    assert data["total"] == 6
    assert {t["bank"] for t in data["results"]} == {"amex", "monzo"}
    assert [t["date"] for t in data["results"]] == sorted(t["date"] for t in data["results"])
    assert all(not t["pending"] for t in data["results"])
    assert data["window"] == {"since": "2026-10-01", "before": "2026-10-31"}


def test_list_transactions_date_range_is_passed_to_the_provider(service, providers):
    run(service.list_transactions("monzo", since="2026-10-02", before="2026-10-03"))
    call = providers["monzo"].calls[-1]
    assert call[2:] == (date(2026, 10, 2), date(2026, 10, 3), False)


def test_list_transactions_range_limits(service):
    with pytest.raises(InvalidRequestError):
        run(service.list_transactions(since="2026-10-05", before="2026-10-01"))
    with pytest.raises(InvalidRequestError):
        run(service.list_transactions(since="2020-01-01", before="2026-10-01"))


def test_list_transactions_paging(service):
    args = {"bank": "monzo", "since": "2026-10-01", "before": "2026-10-31", "limit": 2}
    first = run(service.list_transactions(**args))
    assert [t["id"] for t in first["results"]] == ["monzo-0", "monzo-1"]
    assert first["total"] == 3 and first["next_offset"] == 2
    second = run(service.list_transactions(**args, offset=first["next_offset"]))
    assert [t["id"] for t in second["results"]] == ["monzo-2"]
    assert second["next_offset"] is None


@pytest.mark.parametrize("limit,offset", [(0, 0), (501, 0), (10, -1)])
def test_list_transactions_rejects_bad_paging(service, limit, offset):
    with pytest.raises(InvalidRequestError):
        run(service.list_transactions(limit=limit, offset=offset))


def test_list_pending_returns_only_pending(service, monkeypatch):
    monkeypatch.setattr("bank_mcp.models.london_today", lambda: date(2026, 10, 21))
    data = run(service.list_pending("monzo"))
    assert [t["id"] for t in data["results"]] == ["monzo-p"]
    assert data["results"][0]["pending"] is True


def test_list_pending_all_banks_and_paging(service, monkeypatch):
    monkeypatch.setattr("bank_mcp.models.london_today", lambda: date(2026, 10, 21))
    data = run(service.list_pending(limit=1))
    assert data["total"] == 2 and data["next_offset"] == 1


def test_unknown_bank_is_rejected(service):
    with pytest.raises(InvalidRequestError, match="No bank is linked as 'nope'"):
        run(service.list_accounts("nope"))


def test_no_banks_linked(tmp_path):
    empty = BankService(BankTokenStore(FileTokenStore(tmp_path / "t.json")), lambda n: FakeProvider(n))
    with pytest.raises(NotConnectedError):
        run(empty.list_accounts())
    assert empty.list_connections() == {"connections": []}


def test_expired_consent_names_the_bank(banks, service):
    banks.save("monzo", {"refresh_token": "r1"}, PAST)
    with pytest.raises(ReconnectRequiredError, match="Reconnect required for 'monzo'"):
        run(service.list_accounts("monzo"))


def test_revoked_consent_from_provider_names_the_bank(service, providers):
    providers["fail"] = {"amex": ConsentExpiredError("revoked")}
    with pytest.raises(ReconnectRequiredError, match="Reconnect required for 'amex'"):
        run(service.get_balance("amex"))


def test_reconnect_error_through_the_server_is_a_tool_error(banks, service):
    banks.save("monzo", {"refresh_token": "r1"}, PAST)
    with pytest.raises(ToolError, match="Reconnect required for 'monzo'"):
        run(build_server(service).call_tool("list_accounts", {"bank": "monzo"}))


def test_all_banks_query_keeps_working_banks_and_reports_the_one_to_reconnect(banks, service):
    banks.save("monzo", {"refresh_token": "r1"}, PAST)
    data = run(service.list_transactions(since="2026-10-01", before="2026-10-31"))
    assert {t["bank"] for t in data["results"]} == {"amex"}
    assert [(e["bank"], e["error"]) for e in data["errors"]] == [("monzo", "reconnect_required")]
    assert "Reconnect required for 'monzo'" in data["errors"][0]["message"]


def test_other_provider_errors_are_per_bank_when_querying_all(service, providers):
    providers["fail"] = {"amex": ProviderError("bank is down")}
    data = run(service.list_accounts())
    assert {r["bank"] for r in data["results"]} == {"monzo"}
    assert data["errors"][0]["bank"] == "amex" and data["errors"][0]["error"] == "provider_error"
    with pytest.raises(ProviderError):
        run(service.list_accounts("amex"))
