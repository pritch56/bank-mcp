"""KAN-22: Card model parsing and per-bank token storage."""

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta

import pytest

from bank_mcp.bank_store import BankTokenStore, normalise_nickname
from bank_mcp.errors import InvalidRequestError, TokenStoreError
from bank_mcp.models import Account, Balance, Card, Transaction
from bank_mcp.token_store import FileTokenStore, TokenStore

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return BankTokenStore(FileTokenStore(tmp_path / "tokens.json"))


# ------------------------------------------------------------------ models


def test_card_parses_a_truelayer_card():
    card = Card.from_truelayer(
        {
            "account_id": "c1",
            "display_name": "  Gold   Card ",
            "currency": "gbp",
            "card_type": "CREDIT",
            "card_network": "visa",
            "partial_card_number": "1234",
        },
        provider="truelayer",
    )
    assert card.to_dict() == {
        "id": "c1",
        "card_id": "c1",
        "name": "Gold Card",
        "currency": "GBP",
        "card_type": "credit_card",
        "card_network": "VISA",
        "last4": "1234",
        "provider": "truelayer",
        "source": "bank",
    }


def test_card_defaults_and_charge_type():
    card = Card.from_truelayer({"account_id": "c2", "card_type": "CHARGE"})
    assert (card.name, card.currency, card.card_type, card.last4, card.card_network) == (
        "Card",
        "GBP",
        "charge_card",
        None,
        None,
    )


def test_card_requires_an_id():
    with pytest.raises(KeyError):
        Card.from_truelayer({"display_name": "x"})


def test_account_balance_and_transaction_models_exist():
    assert Account("a", "Current", "GBP").to_dict()["id"] == "a"
    assert Balance("a", "GBP", -500).to_dict()["balance_minor"] == -500
    assert Transaction("t", "a", "2026-10-24", -250, "GBP", "Coffee").to_dict()["amount_minor"] == -250


# ------------------------------------------------------------ token storage


def test_two_banks_are_stored_and_read_independently(store):
    store.save("monzo", {"refresh_token": "m-refresh"}, expires_at="2027-01-01T00:00:00Z")
    store.save("amex", {"refresh_token": "a-refresh"}, expires_at="2026-12-31T00:00:00Z")
    assert store.load("monzo") == {"refresh_token": "m-refresh"}
    assert store.load("amex") == {"refresh_token": "a-refresh"}
    assert store.nicknames() == ["amex", "monzo"]
    assert store.expires_at("monzo") == "2027-01-01T00:00:00Z"
    assert store.expires_at("amex") == "2026-12-31T00:00:00Z"


def test_replacing_one_bank_leaves_the_other_untouched(store):
    store.save("monzo", {"refresh_token": "old"})
    store.save("amex", {"refresh_token": "a"})
    store.save("monzo", {"refresh_token": "new"})
    assert store.load("monzo") == {"refresh_token": "new"}
    assert store.load("amex") == {"refresh_token": "a"}


def test_remove_forgets_only_that_bank(store):
    store.save("monzo", {"t": 1})
    store.save("amex", {"t": 2})
    assert store.remove("monzo") is True
    assert store.load("monzo") is None
    assert store.load("amex") == {"t": 2}
    assert store.remove("monzo") is False


def test_unknown_bank_loads_as_none(store):
    assert store.load("nope") is None
    assert store.expires_at("nope") is None


def test_nicknames_are_case_insensitive_and_validated(store):
    store.save("Monzo", {"t": 1})
    assert store.load("monzo") == {"t": 1}
    for bad in ("", "a b", "-x", "x" * 33, "../etc"):
        with pytest.raises(InvalidRequestError):
            normalise_nickname(bad)


def test_expiry(store):
    store.save("soon", {"t": 1}, expires_at=NOW + timedelta(days=1))
    store.save("past", {"t": 1}, expires_at=(NOW - timedelta(seconds=1)).isoformat())
    store.save("never", {"t": 1})
    assert not store.is_expired("soon", NOW)
    assert store.is_expired("past", NOW)
    assert not store.is_expired("never", NOW)
    assert store.is_expired("soon", NOW + timedelta(days=2))


def test_expiry_accepts_unix_seconds_and_rejects_garbage(store):
    store.save("unix", {"t": 1}, expires_at=NOW.timestamp())
    assert store.expires_at("unix") == "2026-10-24T12:00:00Z"
    with pytest.raises(TokenStoreError):
        store.save("bad", {"t": 1}, expires_at="not a date")


def test_for_bank_is_a_token_store_and_keeps_the_expiry(store):
    store.save("monzo", {"refresh_token": "1"}, expires_at="2027-01-01T00:00:00Z")
    store.save("amex", {"refresh_token": "a"})
    view = store.for_bank("monzo")
    assert isinstance(view, TokenStore)
    assert view.load() == {"refresh_token": "1"}
    view.save({"refresh_token": "2"})  # a refresh rotated the token
    assert store.load("monzo") == {"refresh_token": "2"}
    assert store.expires_at("monzo") == "2027-01-01T00:00:00Z"
    assert store.load("amex") == {"refresh_token": "a"}
    view.clear()
    assert store.nicknames() == ["amex"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_token_file_is_owner_only(tmp_path):
    path = tmp_path / "nested" / "tokens.json"
    s = BankTokenStore(FileTokenStore(path))
    s.save("monzo", {"t": 1})
    s.save("amex", {"t": 2})  # a second write must keep the mode too
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()).get("banks", {}).keys() == {"monzo", "amex"}


def test_token_file_is_written_and_readable(tmp_path):
    path = tmp_path / "tokens.json"
    BankTokenStore(FileTokenStore(path)).save("monzo", {"t": 1})
    assert BankTokenStore(FileTokenStore(path)).load("monzo") == {"t": 1}
