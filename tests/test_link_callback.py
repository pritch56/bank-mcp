"""KAN-25: the real loopback callback server used by bank-mcp-link (browser is mocked)."""

import socket
import threading
import urllib.error
import urllib.request

import pytest

from bank_mcp import link
from bank_mcp.errors import ConfigError, InvalidRequestError


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_flow(monkeypatch, port, visit):
    """Run wait_for_browser_callback; `visit(base_url)` plays the browser's redirect."""
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(link.webbrowser, "open", lambda url: threading.Thread(target=visit, args=(base,)).start())
    return link.wait_for_browser_callback("https://auth.example/?x=1", f"http://localhost:{port}/callback", "st")


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_callback_query_is_returned(monkeypatch):
    port = free_port()
    params = run_flow(monkeypatch, port, lambda base: get(f"{base}/callback?code=abc&state=st"))
    assert params == {"code": "abc", "state": "st"}


def test_wrong_path_gets_404_and_flow_keeps_waiting(monkeypatch):
    port = free_port()
    statuses = []

    def visit(base):
        statuses.append(get(f"{base}/favicon.ico"))
        get(f"{base}/callback?error=access_denied")

    assert run_flow(monkeypatch, port, visit) == {"error": "access_denied"}
    assert statuses == [404]


def test_port_in_use_is_a_config_error(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        monkeypatch.setattr(link.webbrowser, "open", lambda url: None)
        with pytest.raises(ConfigError):
            link.wait_for_browser_callback("https://auth.example/", f"http://localhost:{port}/callback", "st")


def test_timeout_is_reported(monkeypatch):
    port = free_port()
    monkeypatch.setattr(link, "CALLBACK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(link.webbrowser, "open", lambda url: None)
    with pytest.raises(InvalidRequestError, match="Timed out"):
        link.wait_for_browser_callback("https://auth.example/", f"http://localhost:{port}/callback", "st")
