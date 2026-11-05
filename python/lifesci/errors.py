"""Typed errors for the lifesci tool layer.

Every user-visible failure is a :class:`LifesciError` carrying:

* ``code`` — stable machine-readable identifier (``HTTP_429``, ``TOOL_MISSING``…)
  so the agent can branch programmatically;
* ``message`` — actionable human/model-readable text;
* ``retriable`` — whether retrying later could plausibly succeed;
* ``details`` — JSON-serialisable diagnostic context;
* ``fallbacks`` — suggested alternate strategies/sources (never auto-executed).

The CLI converts these into the ``ok: false`` error envelope and a deterministic
exit code; agent tools surface the same code and message.
"""

from __future__ import annotations

from typing import Any, Mapping


class LifesciError(Exception):
    """Base class for all expected lifesci failures."""

    code: str = "INTERNAL"
    exit_code: int = 7
    retriable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retriable: bool | None = None,
        details: Mapping[str, Any] | None = None,
        fallbacks: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retriable is not None:
            self.retriable = retriable
        self.details: dict[str, Any] = dict(details or {})
        self.fallbacks: list[str] = list(fallbacks or [])

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retriable": self.retriable,
        }
        if self.details:
            payload["details"] = self.details
        if self.fallbacks:
            payload["fallbacks"] = self.fallbacks
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class UsageError(LifesciError):
    """Invalid arguments, unknown command, or schema validation failure."""

    code = "USAGE"
    exit_code = 2


class InvalidInputError(UsageError):
    """The command envelope is well-formed but a value is invalid."""

    code = "INVALID_INPUT"


class NotFoundError(LifesciError):
    """A requested record/sequence/structure does not exist (or is unavailable)."""

    code = "NOT_FOUND"
    exit_code = 3


class OfflineCacheMiss(LifesciError):
    """Offline mode was requested but the payload is not in the cache."""

    code = "OFFLINE_CACHE_MISS"
    exit_code = 4

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retriable", False)
        super().__init__(message, **kwargs)


class CacheCorruptError(LifesciError):
    """A cached payload no longer matches its recorded SHA-256."""

    code = "CACHE_CORRUPT"
    exit_code = 4


class PortalError(LifesciError):
    """A remote portal failed after the retry budget was exhausted."""

    code = "HTTP_ERROR"
    exit_code = 5
    retriable = True


class RateLimitedError(PortalError):
    """The portal explicitly rate-limited us (HTTP 429/503)."""

    code = "RATE_LIMITED"


class ToolMissingError(LifesciError):
    """An optional external tool (foldseek, pymol, …) is not installed."""

    code = "TOOL_MISSING"
    exit_code = 6


class DataError(LifesciError):
    """Downloaded or local data could not be parsed as expected."""

    code = "DATA_INVALID"
    exit_code = 5


class ConfigError(LifesciError):
    """Plugin/CLI configuration is invalid or incomplete."""

    code = "CONFIG_ERROR"
    exit_code = 2


class RemoteDisabledError(LifesciError):
    """Remote execution was requested but is not enabled/configured (opt-in)."""

    code = "REMOTE_DISABLED"
    exit_code = 8


class RemoteError(LifesciError):
    """A configured remote execution failed."""

    code = "REMOTE_ERROR"
    exit_code = 8
    retriable = True


#: Map from exit code to a short description, used by the CLI help and docs.
EXIT_CODE_MEANINGS: dict[int, str] = {
    0: "success",
    2: "usage / invalid input",
    3: "not found",
    4: "offline cache miss / corrupt cache",
    5: "portal or data error",
    6: "required external tool missing",
    7: "internal error",
    8: "remote execution error",
}
