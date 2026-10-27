"""KAN-26: opt-in smoke test of every MCP tool against TrueLayer's sandbox test bank.

Skipped unless BANK_MCP_SANDBOX=1. Link the sandbox bank first (needs a browser, once):

    TRUELAYER_ENV=sandbox TRUELAYER_CLIENT_ID=... TRUELAYER_CLIENT_SECRET=... \
        bank-mcp-link --provider mock --name sandbox

then run:

    BANK_MCP_SANDBOX=1 TRUELAYER_ENV=sandbox TRUELAYER_CLIENT_ID=... TRUELAYER_CLIENT_SECRET=... \
        uv run pytest tests/test_sandbox_smoke.py

BANK_MCP_SANDBOX_BANK names the linked bank (default "sandbox"). Tokens come from the normal token store.
"""

import os

import pytest

from bank_mcp.bank_store import BankTokenStore
from bank_mcp.server import BankService, build_server
from bank_mcp.token_store import FileTokenStore
from tests.conftest import run

pytestmark = pytest.mark.skipif(os.environ.get("BANK_MCP_SANDBOX") != "1", reason="set BANK_MCP_SANDBOX=1 to run")

BANK = os.environ.get("BANK_MCP_SANDBOX_BANK", "sandbox")


@pytest.fixture
def server():
    env = {**os.environ, "TRUELAYER_ENV": "sandbox"}
    banks = BankTokenStore(FileTokenStore())
    if BANK not in banks.nicknames():
        pytest.fail(
            f"No bank linked as '{BANK}'. Run `bank-mcp-link --provider mock --name {BANK}` with TRUELAYER_ENV=sandbox."
        )
    from bank_mcp.server import truelayer_factory

    return build_server(BankService(banks, truelayer_factory(banks, env)))


def call(server, tool, **args):
    return run(server.call_tool(tool, args)).structured_content


def test_every_tool_against_the_sandbox_bank(server):
    connections = call(server, "list_connections")
    assert BANK in str(connections)

    accounts = call(server, "list_accounts", bank=BANK)["results"]
    assert accounts, "sandbox bank returned no accounts or cards"
    account_id = accounts[0]["account_id"]

    balances = call(server, "get_balance", bank=BANK, account_id=account_id)["results"]
    assert balances

    txns = call(server, "list_transactions", bank=BANK, account_id=account_id, include_pending=True)
    assert "results" in txns

    pending = call(server, "list_pending", bank=BANK, account_id=account_id)
    assert "results" in pending
