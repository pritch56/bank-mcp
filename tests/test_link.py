"""KAN-23: the bank-mcp-link command, with a mocked OAuth callback and mocked token endpoint."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from bank_mcp.bank_store import BankTokenStore
from bank_mcp.link import main
from bank_mcp.token_store import FileTokenStore

ENV = {"TRUELAYER_CLIENT_ID": "cid", "TRUELAYER_CLIENT_SECRET": "secret"}
SANDBOX_TOKEN_URL = "https://auth.truelayer-sandbox.com/connect/token"
LIVE_TOKEN_URL = "https://auth.truelayer.com/connect/token"
TOKEN_BODY = {"access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600}


@pytest.fixture
def banks(tmp_path):
    return BankTokenStore(FileTokenStore(tmp_path / "tokens.json"))


def approving(code="the-code", seen=None):
    """A stand-in for the browser: records the auth URL, then 'redirects back' with a code."""

    def waiter(auth_url, redirect_uri, state):
        if seen is not None:
            seen.append((auth_url, redirect_uri))
        return {"code": code, "state": state}

    return waiter


def link(banks, *extra, waiter=None, **kwargs):
    argv = ["--provider", "mock", "--name", "Monzo", *extra]
    return main(argv, banks=banks, wait_for_callback=waiter or approving(), env=ENV, **kwargs)


@respx.mock
def test_link_stores_tokens_under_the_nickname(banks):
    route = respx.post(SANDBOX_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    seen = []
    assert link(banks, "--sandbox", waiter=approving(seen=seen)) == 0

    assert banks.nicknames() == ["monzo"]
    tokens = banks.load("monzo")
    assert tokens["refresh_token"] == "ref-1"
    assert tokens["access_token"] == "acc-1"
    assert banks.expires_at("monzo") is not None and not banks.is_expired("monzo")

    auth_url, redirect_uri = seen[0]
    query = parse_qs(urlparse(auth_url).query)
    assert auth_url.startswith("https://auth.truelayer-sandbox.com/")
    assert query["providers"] == ["mock"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["code_challenge_method"] == ["S256"]

    sent = json.loads(route.calls.last.request.content)
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "the-code"
    assert sent["redirect_uri"] == redirect_uri
    assert sent["code_verifier"]


@respx.mock
def test_link_without_sandbox_uses_live_endpoints(banks):
    route = respx.post(LIVE_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    assert link(banks) == 0
    assert route.called


@respx.mock
def test_linking_a_second_bank_keeps_the_first(banks):
    respx.post(SANDBOX_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    assert link(banks, "--sandbox") == 0
    assert (
        main(
            ["--provider", "mock", "--name", "amex", "--sandbox"],
            banks=banks,
            wait_for_callback=approving(),
            env=ENV,
        )
        == 0
    )
    assert banks.nicknames() == ["amex", "monzo"]


@respx.mock
def test_list_shows_linked_banks(banks, capsys):
    assert main(["--list"], banks=banks) == 0
    assert "No banks linked." in capsys.readouterr().out

    respx.post(SANDBOX_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    link(banks, "--sandbox")
    capsys.readouterr()
    assert main(["--list"], banks=banks) == 0
    out = capsys.readouterr().out
    assert out.startswith("monzo\tlinked\tconsent until ")


@respx.mock
def test_remove_forgets_only_that_bank(banks, capsys):
    respx.post(SANDBOX_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    link(banks, "--sandbox")
    main(["--provider", "mock", "--name", "amex", "--sandbox"], banks=banks, wait_for_callback=approving(), env=ENV)

    assert main(["--remove", "Monzo"], banks=banks) == 0
    assert banks.nicknames() == ["amex"]
    assert "Removed 'monzo'" in capsys.readouterr().out


def test_remove_unknown_bank_fails(banks, capsys):
    assert main(["--remove", "nope"], banks=banks) == 1
    assert "No bank is linked as 'nope'" in capsys.readouterr().err


def test_denied_sign_in_stores_nothing(banks, capsys):
    def denied(auth_url, redirect_uri, state):
        return {"error": "access_denied", "state": state}

    assert link(banks, waiter=denied) == 1
    assert "access_denied" in capsys.readouterr().err
    assert banks.nicknames() == []


@respx.mock
def test_state_mismatch_is_rejected_before_any_token_request(banks, capsys):
    route = respx.post(LIVE_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_BODY))
    assert link(banks, waiter=lambda *_: {"code": "c", "state": "forged"}) == 1
    assert "state mismatch" in capsys.readouterr().err
    assert not route.called
    assert banks.nicknames() == []


def test_missing_code_is_rejected(banks, capsys):
    assert link(banks, waiter=lambda _u, _r, state: {"state": state}) == 1
    assert "authorisation code" in capsys.readouterr().err
    assert banks.nicknames() == []


@respx.mock
def test_rejected_code_exchange_stores_nothing(banks, capsys):
    respx.post(LIVE_TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_authorization_code"}))
    assert link(banks) == 1
    assert "authorisation code" in capsys.readouterr().err
    assert banks.nicknames() == []


def test_missing_credentials_fail_cleanly(banks, capsys):
    code = main(["--provider", "mock", "--name", "x"], banks=banks, wait_for_callback=approving(), env={})
    assert code == 1
    assert "TRUELAYER_CLIENT_ID" in capsys.readouterr().err


def test_invalid_nickname_is_rejected(banks, capsys):
    code = main(["--provider", "mock", "--name", "bad name!"], banks=banks, wait_for_callback=approving(), env=ENV)
    assert code == 1
    assert "nickname" in capsys.readouterr().err


def test_linking_requires_provider_and_name(banks):
    with pytest.raises(SystemExit):
        main(["--name", "x"], banks=banks)
