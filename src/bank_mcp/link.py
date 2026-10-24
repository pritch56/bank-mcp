"""`bank-mcp-link`: link a bank through TrueLayer's browser sign-in, and list or remove linked banks.

    bank-mcp-link --provider <id> --name <nickname> [--sandbox]   link a bank
    bank-mcp-link --list                                          show linked banks
    bank-mcp-link --remove <nickname>                             forget a linked bank

Linking opens TrueLayer's sign-in page in the browser and waits for the redirect back to a
short-lived server on 127.0.0.1 (the redirect URI must be registered in the TrueLayer Console).
Tokens are stored per nickname in the owner-only token file (see bank_store).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import secrets
import sys
import threading
import webbrowser
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .bank_store import BankTokenStore, normalise_nickname
from .errors import BankMcpError, ConfigError, InvalidRequestError
from .providers.truelayer import SCOPES, TrueLayerConfig, TrueLayerProvider
from .token_store import MemoryTokenStore

DEFAULT_PORT = 3000
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT_SECONDS = 300
# Open Banking consent lasts 90 days; a refresh does not extend it.
CONSENT_DAYS = 90

# (authorisation URL, redirect URI, expected state) -> query parameters of the redirect back.
CallbackWaiter = Callable[[str, str, str], Mapping[str, str]]


def build_auth_url(config: TrueLayerConfig, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = {
        "response_type": "code",
        "client_id": config.client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "providers": config.provider_id,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.auth_base}/?{urlencode(query)}"


def wait_for_browser_callback(auth_url: str, redirect_uri: str, state: str) -> Mapping[str, str]:
    """Open `auth_url` in the browser and wait for the redirect to `redirect_uri` on loopback."""
    parsed = urlparse(redirect_uri)
    received: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path != (parsed.path or "/"):
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in parse_qs(url.query).items()})
            body = b"bank-mcp: you can close this tab and return to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args: object) -> None:
            return

    try:
        server = HTTPServer(("127.0.0.1", parsed.port or DEFAULT_PORT), Handler)
    except OSError as exc:
        raise ConfigError(f"Could not listen on port {parsed.port}: {exc.strerror or type(exc).__name__}.") from None
    server.timeout = 1.0
    print(f"Opening your browser to sign in. If it does not open, visit:\n{auth_url}", file=sys.stderr)
    webbrowser.open(auth_url)
    deadline = datetime.now(UTC) + timedelta(seconds=CALLBACK_TIMEOUT_SECONDS)
    try:
        while not done.is_set() and datetime.now(UTC) < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not done.is_set():
        raise InvalidRequestError("Timed out waiting for the bank sign-in to finish. Run the command again.")
    return received


def link_bank(
    banks: BankTokenStore,
    *,
    provider: str,
    name: str,
    sandbox: bool = False,
    port: int = DEFAULT_PORT,
    env: Mapping[str, str] | None = None,
    wait_for_callback: CallbackWaiter = wait_for_browser_callback,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Run the OAuth flow and store the tokens under `name`. Returns the normalised nickname."""
    nickname = normalise_nickname(name)
    settings = dict(os.environ if env is None else env)
    settings["TRUELAYER_PROVIDER_ID"] = provider
    if sandbox:
        settings["TRUELAYER_ENV"] = "sandbox"
    config = TrueLayerConfig.from_env(settings)

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{port}{CALLBACK_PATH}"

    params = wait_for_callback(build_auth_url(config, redirect_uri, state, challenge), redirect_uri, state)
    if params.get("error"):
        raise InvalidRequestError(
            f"The bank sign-in was not completed ({params.get('error')}). Run the command again to retry."
        )
    if not secrets.compare_digest(params.get("state", ""), state):
        raise InvalidRequestError(
            "The sign-in response did not match this request (state mismatch). Nothing was stored."
        )
    code = params.get("code")
    if not code:
        raise InvalidRequestError("The sign-in response did not contain an authorisation code. Nothing was stored.")

    async def exchange() -> dict:
        staging = MemoryTokenStore()
        truelayer = TrueLayerProvider(config, staging, client=client)
        try:
            return await truelayer.exchange_code(code, redirect_uri, verifier)
        finally:
            await truelayer.aclose()

    # Remember which bank and environment this nickname belongs to, so the server can rebuild its provider.
    tokens = {**asyncio.run(exchange()), "provider_id": config.provider_id, "environment": config.environment}
    banks.save(nickname, tokens, expires_at=datetime.now(UTC) + timedelta(days=CONSENT_DAYS))
    return nickname


def format_banks(banks: BankTokenStore) -> list[str]:
    lines = []
    for nickname in banks.nicknames():
        expiry = banks.expires_at(nickname)
        status = "expired" if banks.is_expired(nickname) else "linked"
        lines.append(f"{nickname}\t{status}\t{('consent until ' + expiry) if expiry else 'no recorded expiry'}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bank-mcp-link", description="Link, list and remove banks.")
    parser.add_argument("--provider", help="TrueLayer provider id of the bank (for example 'mock' in the sandbox)")
    parser.add_argument("--name", help="nickname to store the bank under")
    parser.add_argument("--sandbox", action="store_true", help="use the TrueLayer sandbox")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"loopback port for the redirect (default {DEFAULT_PORT})"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true", help="list linked banks")
    actions.add_argument("--remove", metavar="NAME", help="forget a linked bank")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    banks: BankTokenStore | None = None,
    wait_for_callback: CallbackWaiter = wait_for_browser_callback,
    env: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        banks = banks or BankTokenStore()
        if args.list:
            lines = format_banks(banks)
            print("\n".join(lines) if lines else "No banks linked.")
        elif args.remove:
            nickname = normalise_nickname(args.remove)
            if not banks.remove(nickname):
                print(f"No bank is linked as '{nickname}'.", file=sys.stderr)
                return 1
            print(f"Removed '{nickname}'.")
        else:
            if not args.provider or not args.name:
                parser.error("linking needs --provider and --name (or use --list / --remove NAME)")
            nickname = link_bank(
                banks,
                provider=args.provider,
                name=args.name,
                sandbox=args.sandbox,
                port=args.port,
                env=env,
                wait_for_callback=wait_for_callback,
                client=client,
            )
            print(f"Linked '{nickname}'.")
    except BankMcpError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
