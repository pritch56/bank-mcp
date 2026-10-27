"""Per-bank token storage: several linked banks in one owner-only file.

Each bank is kept under a nickname (for example "monzo" or "amex") together with its token
document and the time its consent expires. Banks are stored and read independently, so
re-linking or removing one never touches another.

File layout (PLAINTEXT JSON, protected by file permissions only; see token_store):
    {
      "banks": {
        "<nickname>": {"tokens": {...}, "expires_at": "<ISO 8601 UTC, ends in Z>" | null,
                       "updated_at": "<ISO 8601 UTC>"}
      }
    }

`for_bank(nickname)` returns a `TokenStore` view of one bank, so a provider can be built
against a single bank without knowing about the others.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .errors import InvalidRequestError, TokenStoreError
from .models import utc_z
from .token_store import FileTokenStore

_NICKNAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def normalise_nickname(nickname: str) -> str:
    """Lower-case and validate a bank nickname (1-32 chars of a-z, 0-9, '-' and '_')."""
    name = str(nickname or "").strip().lower()
    if not _NICKNAME_RE.fullmatch(name):
        raise InvalidRequestError(
            "A bank nickname must be 1-32 characters of letters, digits, '-' or '_' and start with a letter or digit."
        )
    return name


class BankTokenStore:
    """Stores token documents for several banks in a single file, keyed by nickname."""

    def __init__(self, store: FileTokenStore | None = None):
        self._file = store or FileTokenStore()

    def _banks(self) -> dict[str, dict[str, Any]]:
        doc = self._file.load() or {}
        banks = doc.get("banks")
        return banks if isinstance(banks, dict) else {}

    def nicknames(self) -> list[str]:
        return sorted(self._banks())

    def save(self, nickname: str, tokens: dict[str, Any], expires_at: str | datetime | float | None = None) -> None:
        """Store (or replace) one bank. `expires_at` is when the bank consent lapses."""
        name = normalise_nickname(nickname)
        banks = self._banks()
        banks[name] = {
            "tokens": dict(tokens),
            "expires_at": _expiry(expires_at),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        self._file.save({"banks": banks})

    def load(self, nickname: str) -> dict[str, Any] | None:
        entry = self._banks().get(normalise_nickname(nickname))
        tokens = entry.get("tokens") if isinstance(entry, dict) else None
        return dict(tokens) if isinstance(tokens, dict) else None

    def expires_at(self, nickname: str) -> str | None:
        entry = self._banks().get(normalise_nickname(nickname))
        return entry.get("expires_at") if isinstance(entry, dict) else None

    def is_expired(self, nickname: str, now: datetime | None = None) -> bool:
        """True when the bank's recorded expiry has passed. No recorded expiry means not expired."""
        stamp = self.expires_at(nickname)
        if not stamp:
            return False
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) <= (now or datetime.now(UTC))

    def remove(self, nickname: str) -> bool:
        """Forget one bank. Returns False when it was not stored."""
        name = normalise_nickname(nickname)
        banks = self._banks()
        if name not in banks:
            return False
        del banks[name]
        self._file.save({"banks": banks})
        return True

    def for_bank(self, nickname: str) -> BankView:
        return BankView(self, normalise_nickname(nickname))


class BankView:
    """A `TokenStore` (load/save/clear) bound to one nickname of a `BankTokenStore`."""

    def __init__(self, parent: BankTokenStore, nickname: str):
        self._parent = parent
        self.nickname = nickname

    def load(self) -> dict[str, Any] | None:
        return self._parent.load(self.nickname)

    def save(self, tokens: dict[str, Any]) -> None:
        # Keep the expiry recorded at link time; a token refresh does not extend the consent.
        self._parent.save(self.nickname, tokens, self._parent.expires_at(self.nickname))

    def clear(self) -> None:
        self._parent.remove(self.nickname)


def _expiry(value: str | datetime | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        value = datetime.fromtimestamp(value, UTC)
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    stamp = utc_z(text)
    if stamp is None:
        raise TokenStoreError("The bank expiry is not a valid ISO 8601 date/time.")
    return stamp
