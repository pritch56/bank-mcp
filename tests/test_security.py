"""KAN-27: security properties - no secrets in logs or tool output, owner-only token file,
read-only scopes. All HTTP is mocked (respx)."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from bank_mcp.bank_store import BankTokenStore
from bank_mcp.link import build_auth_url
from bank_mcp.providers.truelayer import SCOPES, TrueLayerConfig, TrueLayerProvider
from bank_mcp.server import BankService, build_server
from bank_mcp.token_store import FileTokenStore, MemoryTokenStore

from .conftest import BALANCE, CARD, run

AUTH = "https://auth.truelayer.com"
API = "https://api.truelayer.com"
ACCT = CARD["account_id"]

CLIENT_SECRET = "s3cret-client-value"
ACCESS = "SECRET-ACCESS-TOKEN-1"
REFRESH = "SECRET-REFRESH-TOKEN-1"
NEW_ACCESS = "SECRET-ACCESS-TOKEN-2"
NEW_REFRESH = "SECRET-REFRESH-TOKEN-2"
SECRETS = [CLIENT_SECRET, ACCESS, REFRESH, NEW_ACCESS, NEW_REFRESH]

# Everything TrueLayer calls a read scope; payments/standing orders/etc. must never appear.
READ_SCOPES = {"info", "accounts", "cards", "balance", "transactions", "offline_access"}


def config() -> TrueLayerConfig:
    return TrueLayerConfig(client_id="test-client", client_secret=CLIENT_SECRET, provider_id="ob-example")


def make_provider(store, **tokens) -> TrueLayerProvider:
    p = TrueLayerProvider(config(), store, backoff_seconds=0)
    p._kinds = {ACCT: "cards"}
    return p


def stale_store() -> MemoryTokenStore:
    return MemoryTokenStore({"access_token": ACCESS, "access_token_expires_at": 0, "refresh_token": REFRESH})


def assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# ------------------------------------------------------------------ read-only scopes


def test_requested_scopes_are_read_only():
    scopes = set(SCOPES.split())
    assert scopes, "no scopes requested"
    assert scopes <= READ_SCOPES, f"non-read scopes requested: {scopes - READ_SCOPES}"


def test_auth_url_requests_only_read_scopes():
    url = build_auth_url(config(), "http://localhost:3000/callback", "state", "challenge")
    requested = set(parse_qs(urlparse(url).query)["scope"][0].split())
    assert requested == set(SCOPES.split())
    assert requested <= READ_SCOPES
    assert "payments" not in url


# ------------------------------------------------------------------ file permissions


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_token_file_and_folder_are_owner_only(tmp_path):
    path = tmp_path / "bank" / "tokens.json"
    FileTokenStore(path).save({"refresh_token": REFRESH})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_rewrite_keeps_token_file_owner_only(tmp_path):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    store.save({"refresh_token": REFRESH})
    store.save({"refresh_token": NEW_REFRESH})
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_bank_store_file_is_owner_only(tmp_path):
    path = tmp_path / "tokens.json"
    BankTokenStore(FileTokenStore(path)).save("monzo", {"refresh_token": REFRESH}, None)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_windows_token_file_acl_excludes_other_principals(tmp_path):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save({"refresh_token": REFRESH})
    out = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert broad not in out
    assert "(I)" not in out, "ACL still inherits entries from the parent folder"


# ------------------------------------------------------------------ no secrets in logs / errors


@respx.mock
def test_no_secrets_in_logs_or_errors_on_a_successful_refresh(caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": NEW_ACCESS, "expires_in": 3600, "refresh_token": NEW_REFRESH}
        )
    )
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(
        return_value=httpx.Response(200, json={"results": [BALANCE], "status": "Succeeded"})
    )
    balance = run(make_provider(stale_store()).get_balance(ACCT))
    assert_clean(caplog.text)
    assert_clean(repr(balance))


ERROR_RESPONSES = [
    ("invalid_grant", httpx.Response(400, json={"error": "invalid_grant", "refresh_token": REFRESH})),
    ("invalid_client", httpx.Response(401, json={"error": "invalid_client", "client_secret": CLIENT_SECRET})),
    ("rate_limited", httpx.Response(429, headers={"Retry-After": "5"}, text=f"slow down {REFRESH}")),
    ("server_error", httpx.Response(500, text=f"boom {ACCESS} {REFRESH} {CLIENT_SECRET}")),
]


@pytest.mark.parametrize(("label", "response"), ERROR_RESPONSES, ids=[e[0] for e in ERROR_RESPONSES])
@respx.mock
def test_token_endpoint_errors_do_not_leak_secrets(label, response, caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(f"{AUTH}/connect/token").mock(return_value=response)
    with pytest.raises(Exception) as excinfo:
        run(make_provider(stale_store()).get_balance(ACCT))
    assert_clean(str(excinfo.value))
    assert_clean(repr(excinfo.value))
    assert_clean(caplog.text)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
@respx.mock
def test_api_errors_do_not_leak_secrets(status, caplog):
    caplog.set_level(logging.DEBUG)
    store = MemoryTokenStore(
        {"access_token": ACCESS, "access_token_expires_at": time.time() + 3000, "refresh_token": REFRESH}
    )
    respx.post(f"{AUTH}/connect/token").mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(
        return_value=httpx.Response(status, json={"error": "x", "error_description": f"{ACCESS} {REFRESH}"})
    )
    with pytest.raises(Exception) as excinfo:
        run(make_provider(store).get_balance(ACCT))
    assert_clean(str(excinfo.value))
    assert_clean(caplog.text)


@respx.mock
def test_network_failure_does_not_leak_secrets(caplog):
    caplog.set_level(logging.DEBUG)
    respx.post(f"{AUTH}/connect/token").mock(side_effect=httpx.ConnectError(f"cannot reach {CLIENT_SECRET}"))
    with pytest.raises(Exception) as excinfo:
        run(make_provider(stale_store()).get_balance(ACCT))
    assert_clean(str(excinfo.value))
    assert_clean(caplog.text)


@respx.mock
def test_token_store_save_failure_does_not_leak_secrets(caplog):
    caplog.set_level(logging.DEBUG)

    class Broken(MemoryTokenStore):
        def save(self, tokens):
            raise OSError(f"disk full while writing {tokens['refresh_token']}")

    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": NEW_ACCESS, "expires_in": 3600, "refresh_token": NEW_REFRESH}
        )
    )
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(
        return_value=httpx.Response(200, json={"results": [BALANCE], "status": "Succeeded"})
    )
    provider = make_provider(Broken({"refresh_token": REFRESH}))
    run(provider.get_balance(ACCT))
    assert provider.pending_warning()
    assert_clean(provider.pending_warning())
    assert_clean(caplog.text)


def test_corrupt_token_file_error_does_not_echo_contents(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(f'{{"refresh_token": "{REFRESH}"', encoding="utf-8")  # truncated JSON
    with pytest.raises(Exception) as excinfo:
        FileTokenStore(path).load()
    assert_clean(str(excinfo.value))


# ------------------------------------------------------------------ tool output


@pytest.fixture
def server(tmp_path):
    banks = BankTokenStore(FileTokenStore(tmp_path / "tokens.json"))
    expiry = datetime.now(UTC) + timedelta(days=60)
    banks.save("monzo", {"refresh_token": REFRESH, "access_token": ACCESS}, expiry)

    def factory(nickname):
        return make_provider(MemoryTokenStore({"refresh_token": REFRESH, "access_token": ACCESS}))

    return build_server(BankService(banks, factory, max_range_days=366))


def dump(result) -> str:
    return repr(result.structured_content) + repr(result.content)


@respx.mock
def test_tool_results_do_not_contain_tokens(server):
    respx.post(f"{AUTH}/connect/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": NEW_ACCESS, "expires_in": 3600, "refresh_token": NEW_REFRESH}
        )
    )
    respx.get(f"{API}/data/v1/accounts").mock(
        return_value=httpx.Response(501, json={"error": "endpoint_not_supported"})
    )
    respx.get(f"{API}/data/v1/cards").mock(
        return_value=httpx.Response(200, json={"results": [CARD], "status": "Succeeded"})
    )
    respx.get(f"{API}/data/v1/cards/{ACCT}/balance").mock(
        return_value=httpx.Response(200, json={"results": [BALANCE], "status": "Succeeded"})
    )
    for tool, args in [("list_connections", {}), ("list_accounts", {}), ("get_balance", {"account_id": ACCT})]:
        assert_clean(dump(run(server.call_tool(tool, args))))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_grant", "detail": f"{REFRESH} {CLIENT_SECRET}"}),
        httpx.Response(500, text=f"{ACCESS} {REFRESH} {CLIENT_SECRET}"),
    ],
    ids=["invalid_grant", "server_error"],
)
@respx.mock
def test_tool_error_paths_do_not_contain_tokens(server, response):
    respx.post(f"{AUTH}/connect/token").mock(return_value=response)
    respx.get(f"{API}/data/v1/accounts").mock(return_value=response)
    respx.get(f"{API}/data/v1/cards").mock(return_value=response)
    for args in ({"bank": "monzo"}, {}):
        try:
            result = run(server.call_tool("list_accounts", args))
        except ToolError as exc:
            assert_clean(str(exc))
        else:
            assert_clean(dump(result))
