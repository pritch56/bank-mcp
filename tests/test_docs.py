"""The repo docs exist, cover what users need, and only link to files that exist."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md"]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", DOCS)
def test_doc_exists_and_is_not_empty(name: str) -> None:
    assert read(name).strip()


@pytest.mark.parametrize(
    "heading",
    ["## Setup", "## Get TrueLayer credentials", "## Link a bank", "### Claude Desktop", "### Claude Code"],
)
def test_readme_covers_required_sections(heading: str) -> None:
    assert heading in read("README.md").splitlines()


def test_readme_config_snippets_are_complete() -> None:
    readme = read("README.md")
    assert '"mcpServers"' in readme  # Claude Desktop
    assert "claude mcp add" in readme  # Claude Code
    for var in ("TRUELAYER_CLIENT_ID", "TRUELAYER_CLIENT_SECRET", "http://localhost:3000/callback"):
        assert var in readme


def test_documented_commands_are_real_entry_points() -> None:
    scripts = tomllib.loads(read("pyproject.toml"))["project"]["scripts"]
    readme = read("README.md")
    for command in re.findall(r"uv run (bank-mcp(?:-link)?)\b", readme):
        assert command in scripts


def test_documented_link_flags_exist() -> None:
    from bank_mcp.link import build_parser

    flags = set(build_parser()._option_string_actions)
    for flag in set(re.findall(r"bank-mcp-link [^\n]*?(--[a-z-]+)", read("README.md"))):
        assert flag in flags


def test_changelog_has_current_version() -> None:
    version = tomllib.loads(read("pyproject.toml"))["project"]["version"]
    assert f"## [{version}]" in read("CHANGELOG.md")


@pytest.mark.parametrize("name", [*DOCS, "SECURITY_REVIEW.md"])
def test_relative_links_resolve(name: str) -> None:
    for target in re.findall(r"\]\((?!https?:|mailto:|#)([^)#\s]+)", read(name)):
        assert (ROOT / target).exists(), f"{name} links to missing {target}"
