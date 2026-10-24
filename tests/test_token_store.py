"""KAN-25: token store persistence, failure paths and pluggable-store loading."""

import json
import subprocess

import pytest

from bank_mcp import token_store
from bank_mcp.errors import ConfigError, TokenStoreError
from bank_mcp.token_store import (
    FileTokenStore,
    MemoryTokenStore,
    build_token_store,
    default_token_path,
    restrict_to_current_user,
)

DOC = {"access_token": "acc", "refresh_token": "ref"}


class GoodStore:
    def load(self):
        return None

    def save(self, tokens):
        pass


class NotAStore:
    pass


def test_file_store_round_trip_and_clear(tmp_path):
    store = FileTokenStore(tmp_path / "sub" / "tokens.json")
    assert store.load() is None
    store.save(DOC)
    assert store.load() == DOC
    store.clear()
    assert store.load() is None
    store.clear()  # clearing twice is harmless


def test_file_store_leaves_no_temp_files_behind(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(DOC)
    store.save({**DOC, "access_token": "acc-2"})
    assert [p.name for p in tmp_path.iterdir()] == ["tokens.json"]


def test_corrupt_file_is_a_token_store_error_without_leaking_contents(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json refresh_token=SECRET-VALUE", encoding="utf-8")
    with pytest.raises(TokenStoreError) as exc:
        FileTokenStore(path).load()
    assert "SECRET-VALUE" not in str(exc.value)


def test_non_object_json_loads_as_nothing(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(["a"]), encoding="utf-8")
    assert FileTokenStore(path).load() is None


def test_unreadable_path_is_a_token_store_error(tmp_path):
    with pytest.raises(TokenStoreError):
        FileTokenStore(tmp_path).load()  # a directory, not a file


def test_save_failure_is_a_token_store_error_and_cleans_up(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(token_store.os, "replace", boom)
    with pytest.raises(TokenStoreError):
        store.save(DOC)
    assert list(tmp_path.iterdir()) == []


def test_unserialisable_document_is_a_token_store_error(tmp_path):
    with pytest.raises(TokenStoreError):
        FileTokenStore(tmp_path / "tokens.json").save({"bad": object()})


def test_clear_failure_is_a_token_store_error(tmp_path):
    with pytest.raises(TokenStoreError):
        FileTokenStore(tmp_path).clear()  # unlinking a directory fails on every platform


def test_memory_store_copies_what_it_is_given():
    initial = dict(DOC)
    store = MemoryTokenStore(initial)
    initial["access_token"] = "changed"
    loaded = store.load()
    assert loaded == DOC
    loaded["access_token"] = "mutated"
    assert store.load() == DOC
    store.clear()
    assert store.load() is None
    assert MemoryTokenStore().load() is None


def test_default_path_and_env_override(tmp_path, monkeypatch):
    assert default_token_path().name == "tokens.json"
    monkeypatch.setenv("BANK_TOKEN_FILE", str(tmp_path / "custom.json"))
    assert FileTokenStore().path == tmp_path / "custom.json"


def test_build_token_store_variants(monkeypatch):
    assert isinstance(build_token_store("file"), FileTokenStore)
    assert isinstance(build_token_store(""), FileTokenStore)
    assert isinstance(build_token_store("memory"), MemoryTokenStore)
    monkeypatch.setenv("BANK_TOKEN_STORE", "memory")
    assert isinstance(build_token_store(), MemoryTokenStore)
    assert isinstance(build_token_store("tests.test_token_store:GoodStore"), GoodStore)


@pytest.mark.parametrize(
    "spec",
    ["nonsense", "no_such_module_xyz:Thing", "tests.test_token_store:Missing", "tests.test_token_store:NotAStore"],
)
def test_build_token_store_rejects_bad_specs(spec):
    with pytest.raises(ConfigError):
        build_token_store(spec)


def test_restrict_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    path = tmp_path / "f"
    path.write_text("x")

    def boom(*_a, **_k):
        raise OSError("nope")

    monkeypatch.setattr(token_store.os, "chmod", boom)
    monkeypatch.setattr(token_store.subprocess, "run", boom)
    with caplog.at_level("WARNING", logger="bank_mcp.token_store"):
        assert restrict_to_current_user(path) is False
    assert "could not restrict" in caplog.text


def test_windows_acl_nonzero_exit_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(token_store.sys, "platform", "win32")
    monkeypatch.setattr(token_store.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 5, b"", b""))
    assert restrict_to_current_user(tmp_path / "f") is False


def test_windows_acl_success_grants_current_user_only(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(token_store.sys, "platform", "win32")
    monkeypatch.setattr(
        token_store.subprocess,
        "run",
        lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    assert restrict_to_current_user(tmp_path / "f") is True
    assert "/inheritance:r" in calls[0]
