"""KAN-21: the TrueLayer provider is bank-agnostic and covers /accounts and /cards."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from bank_mcp.errors import AccountNotFoundError, ConfigError, NotSupportedError
from bank_mcp.providers.truelayer import TrueLayerConfig, TrueLayerProvider
from bank_mcp.token_store import MemoryTokenStore

from .conftest import BALANCE, CARD, ME, run

API = "https://api.truelayer.com"
AUTH = "https://auth.truelayer.com"
SANDBOX_API = "https://api.truelayer-sandbox.com"
SANDBOX_AUTH = "https://auth.truelayer-sandbox.com"
BASE_ENV = {"TRUELAYER_CLIENT_ID": "id", "TRUELAYER_CLIENT_SECRET": "secret"}
CURRENT = {
    "account_id": "acc111",
    "account_type": "TRANSACTION",
    "currency": "GBP",
    "display_name": "Everyday Current",
    "account_number": {"number": "12345678", "sort_code": "00-00-00"},
}
ACCOUNT_BALANCE = {
    "currency": "GBP",
    "available": 90.5,
    "current": 100.25,
    "overdraft": 250,
    "update_timestamp": "2026-10-08T07:00:00Z",
}
ACCOUNT_TXNS = [
    {
        "transaction_id": "t1",
        "normalised_provider_transaction_id": "n1",
        "timestamp": "2026-10-02T09:00:00+00:00",
        "description": "SALARY",
        "amount": 1500.0,
        "currency": "GBP",
        "transaction_type": "CREDIT",
    },
    {  # no transaction_type: the sign of an account amount is kept as-is
        "transaction_id": "t2",
        "normalised_provider_transaction_id": "n2",
        "timestamp": "2026-10-03T09:00:00+00:00",
        "description": "RENT",
        "amount": -700.0,
        "currency": "GBP",
    },
]
NOT_SUPPORTED = httpx.Response(501, json={"error": "endpoint_not_supported"})


def ok(results) -> httpx.Response:
    return httpx.Response(200, json={"results": results, "status": "Succeeded"})


def make(provider_id="ob-anybank", store=None, **kw) -> TrueLayerProvider:
    cfg = TrueLayerConfig("cid", "csecret", provider_id, **kw)
    store = store or MemoryTokenStore(
        {"access_token": "a1", "access_token_expires_at": time.time() + 3000, "refresh_token": "r1"}
    )
    return TrueLayerProvider(cfg, store, backoff_seconds=0)


# ------------------------------------------------------------- provider id config


def test_provider_id_is_read_from_env_and_normalised():
    cfg = TrueLayerConfig.from_env({**BASE_ENV, "TRUELAYER_PROVIDER_ID": " OB-Nationwide "})
    assert cfg.provider_id == "ob-nationwide"
    assert "ob-nationwide" in repr(cfg)
    assert "csecret" not in repr(cfg)


@pytest.mark.parametrize("bad", ["", "   ", "has space", "../x", "ob_bank!", "-lead"])
def test_provider_id_is_validated(bad):
    with pytest.raises(ConfigError, match="TRUELAYER_PROVIDER_ID"):
        TrueLayerConfig.from_env({**BASE_ENV, "TRUELAYER_PROVIDER_ID": bad})


def test_provider_id_is_mandatory_in_env():
    with pytest.raises(ConfigError, match="TRUELAYER_PROVIDER_ID"):
        TrueLayerConfig.from_env(BASE_ENV)


def test_no_bank_is_hardcoded_in_the_provider_source():
    src = (Path(__file__).parent.parent / "src" / "bank_mcp" / "providers" / "truelayer.py").read_text("utf-8")
    for bank in ("amex", "american express", "nationwide", "monzo", "barclays"):
        assert bank not in src.lower()


@respx.mock
def test_connection_status_warns_when_linked_bank_differs_from_configured_provider():
    respx.get(f"{API}/data/v1/me").mock(return_value=ok([ME]))
    reported = ME["provider"]["provider_id"]
    mismatched = run(make(provider_id="ob-other").connection_status())
    assert reported != "ob-other"
    assert mismatched["connected"] and "TRUELAYER_PROVIDER_ID" in mismatched["warning"]
    matching = run(make(provider_id=reported).connection_status())
    assert "TRUELAYER_PROVIDER_ID" not in (matching["warning"] or "")


# ---------------------------------------------------------- /accounts and /cards


@respx.mock
def test_list_accounts_returns_both_accounts_and_cards():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    accounts = run(make().list_accounts())
    assert [(a.account_id, a.type) for a in accounts] == [
        ("acc111", "current_account"),
        (CARD["account_id"], "credit_card"),
    ]
    assert accounts[0].last4 == "5678"
    assert accounts[0].card_network is None


@respx.mock
def test_a_bank_without_cards_still_lists_its_accounts():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=NOT_SUPPORTED)
    assert [a.account_id for a in run(make().list_accounts())] == ["acc111"]


@respx.mock
def test_a_bank_without_accounts_still_lists_its_cards():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=NOT_SUPPORTED)
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    assert [a.account_id for a in run(make().list_accounts())] == [CARD["account_id"]]


@respx.mock
def test_neither_endpoint_supported_is_an_error():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=NOT_SUPPORTED)
    respx.get(f"{API}/data/v1/cards").mock(return_value=NOT_SUPPORTED)
    with pytest.raises(NotSupportedError):
        run(make().list_accounts())


@respx.mock
def test_account_balance_uses_accounts_endpoint_and_keeps_sign():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    route = respx.get(f"{API}/data/v1/accounts/acc111/balance").mock(return_value=ok([ACCOUNT_BALANCE]))
    bal = run(make().get_balance("acc111"))  # list_accounts runs implicitly to learn the kind
    assert route.called
    assert (bal.balance_minor, bal.available_credit_minor, bal.credit_limit_minor) == (10025, 9050, 25000)


@respx.mock
def test_card_balance_still_uses_cards_endpoint():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=ok([CARD]))
    route = respx.get(f"{API}/data/v1/cards/{CARD['account_id']}/balance").mock(return_value=ok([BALANCE]))
    bal = run(make().get_balance(CARD["account_id"]))
    assert route.called
    assert bal.balance_minor < 0  # card balances: negative = owed


@respx.mock
def test_account_transactions_use_accounts_endpoint_with_account_signs():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=NOT_SUPPORTED)
    txns = respx.get(f"{API}/data/v1/accounts/acc111/transactions").mock(return_value=ok(ACCOUNT_TXNS))
    respx.get(f"{API}/data/v1/accounts/acc111/transactions/pending").mock(return_value=ok([]))
    page = run(make().list_transactions("acc111", date(2026, 10, 1), date(2026, 10, 8)))
    assert txns.called
    assert [(t.description, t.amount_minor) for t in page.transactions] == [("SALARY", 150000), ("RENT", -70000)]


@respx.mock
def test_unknown_account_id_is_not_found():
    respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=NOT_SUPPORTED)
    with pytest.raises(AccountNotFoundError):
        run(make().get_balance("nope"))


# ------------------------------------------------------------------ token refresh


@respx.mock
def test_expired_token_is_refreshed_before_calling_accounts():
    store = MemoryTokenStore(
        {"access_token": "old", "access_token_expires_at": time.time() - 10, "refresh_token": "r1"}
    )
    token = respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600, "refresh_token": "r2"})
    )
    accounts = respx.get(f"{API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    respx.get(f"{API}/data/v1/cards").mock(return_value=NOT_SUPPORTED)
    run(make(store=store).list_accounts())
    assert token.call_count == 1  # refreshed once, reused for the second endpoint
    assert accounts.calls[0].request.headers["Authorization"] == "Bearer new"
    assert store.load()["refresh_token"] == "r2"


# ------------------------------------------------------------- sandbox / live switch


def test_environment_switch_selects_truelayer_urls():
    live = TrueLayerConfig("c", "s", "ob-x")
    sandbox = TrueLayerConfig("c", "s", "mock", environment="sandbox")
    assert (live.api_base, live.auth_base) == (API, AUTH)
    assert (sandbox.api_base, sandbox.auth_base) == (SANDBOX_API, SANDBOX_AUTH)
    env = {**BASE_ENV, "TRUELAYER_PROVIDER_ID": "mock", "TRUELAYER_ENV": "sandbox"}
    assert TrueLayerConfig.from_env(env).api_base == SANDBOX_API
    with pytest.raises(ConfigError, match="TRUELAYER_ENV"):
        TrueLayerConfig("c", "s", "mock", environment="staging")


@respx.mock
def test_sandbox_provider_calls_sandbox_hosts_for_both_endpoints_and_refresh():
    store = MemoryTokenStore({"refresh_token": "r1"})
    token = respx.post(f"{SANDBOX_AUTH}/connect/token").mock(
        return_value=httpx.Response(200, json={"access_token": "sb", "expires_in": 3600})
    )
    accounts = respx.get(f"{SANDBOX_API}/data/v1/accounts").mock(return_value=ok([CURRENT]))
    cards = respx.get(f"{SANDBOX_API}/data/v1/cards").mock(return_value=ok([CARD]))
    run(make("mock", store=store, environment="sandbox").list_accounts())
    assert token.called
    assert accounts.called
    assert cards.called
