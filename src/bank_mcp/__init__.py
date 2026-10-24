"""Unofficial read-only MCP server for bank UK card data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bank-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"

SOURCE = "bank"
USER_AGENT = f"bank-mcp/{__version__}"
