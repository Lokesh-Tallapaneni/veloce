"""Settings — the typed, read-only view of `app.config`.

`app.config` stays a plain `dict` so extensions can add keys freely. The
`Settings` dataclass below is the declaration of the keys Veloce itself reads:
their names, types, defaults and descriptions live here and nowhere else.
`Config.default_config()` is derived from it, and the configuration guide is
checked against it by `tests/test_config_documentation_parity.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields
from typing import TYPE_CHECKING, Any

from veloce._protocol_constants import URL_SCHEME_HTTP
from veloce.config import DEFAULT_MAX_CONCURRENT_CONNECTIONS, DEFAULT_WRITE_BUFFER_HIGH_WATER

if TYPE_CHECKING:  # pragma: no cover
    from veloce.config import Config


def _setting(default: Any, description: str) -> Any:
    """Declare a field whose default and description are read back by the docs check."""
    return field(default=default, metadata={"description": description})


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed, read-only snapshot of the keys Veloce reads from `app.config`.

    Usage::

        app = Veloce()
        app.config["REQUEST_TIMEOUT"] = 10
        app.settings.REQUEST_TIMEOUT  # 10, typed as `float`

    `app.settings` builds a fresh snapshot on each access, so it always reflects
    the current `app.config`; read it once and keep the result when you need
    several keys. Assigning to it raises; change `app.config` instead.
    """

    DEBUG: bool = _setting(False, "Enable debug behaviour (tracebacks, propagation).")
    TESTING: bool = _setting(False, "Mark the app as under test.")
    SECRET_KEY: str | None = _setting(None, "Key for signing sessions and tokens.")
    SERVER_NAME: str | None = _setting(None, "Host (and optional port) used for URL building.")
    PREFERRED_URL_SCHEME: str = _setting(
        URL_SCHEME_HTTP, "Scheme used when generating external URLs."
    )
    MAX_CONTENT_LENGTH: int | None = _setting(
        100 * 1024 * 1024,
        "Maximum request body size in bytes; None is unlimited. The body is buffered, so the default bounds a large-request OOM.",
    )
    MAX_FORM_PARTS: int | None = _setting(
        1000, "Maximum number of form parts (multipart) or fields (urlencoded); None lifts the cap."
    )
    MAX_FORM_PART_SIZE: int = _setting(
        10 * 1024 * 1024,
        "Maximum size of a single form part in bytes, unless a per-kind limit below overrides it.",
    )
    MAX_FORM_FILES: int | None = _setting(
        None, "Maximum number of file parts; None is bounded only by MAX_FORM_PARTS."
    )
    MAX_FORM_FIELDS: int | None = _setting(
        None, "Maximum number of text-field parts; None is bounded only by MAX_FORM_PARTS."
    )
    MAX_FORM_FILE_SIZE: int | None = _setting(
        None, "Per-file size limit in bytes; overrides MAX_FORM_PART_SIZE for file parts."
    )
    MAX_FORM_FIELD_SIZE: int | None = _setting(
        None, "Per-text-field size limit in bytes; overrides MAX_FORM_PART_SIZE for text parts."
    )
    MAX_FORM_FIELD_MEMORY: int | None = _setting(
        None, "Cumulative resident-memory ceiling in bytes across all text fields."
    )
    JSON_SORT_KEYS: bool = _setting(False, "Sort keys when serialising JSON responses.")
    JSONIFY_PRETTYPRINT_REGULAR: bool = _setting(False, "Pretty-print JSON responses.")
    PROPAGATE_EXCEPTIONS: bool | None = _setting(
        None, "Re-raise unhandled exceptions out of dispatch; None follows DEBUG."
    )
    SEND_FILE_MAX_AGE_DEFAULT: int | None = _setting(
        None, "Default Cache-Control max-age for send_file when the caller passes no max_age."
    )
    REQUEST_HANDLER_TIMEOUT: float = _setting(
        30, "Per-handler timeout in seconds, on the built-in serving path."
    )
    KEEP_ALIVE_TIMEOUT: float = _setting(
        75, "Keep-alive timeout in seconds, on the built-in serving path."
    )
    REQUEST_TIMEOUT: float = _setting(
        30, "Request read timeout in seconds, on the built-in serving path."
    )
    MAX_PIPELINED_REQUESTS: int = _setting(
        64,
        "Pipelined requests a connection queues before it stops reading, on the built-in serving path.",
    )
    TCP_KEEPALIVE: bool = _setting(
        True, "Enable OS-level TCP keepalive (SO_KEEPALIVE) on the built-in serving path."
    )
    TCP_KEEPALIVE_IDLE: int | None = _setting(
        None, "Idle seconds before the first keepalive probe (TCP_KEEPIDLE); Linux and macOS only."
    )
    TCP_KEEPALIVE_INTERVAL: int | None = _setting(
        None, "Seconds between keepalive probes (TCP_KEEPINTVL); Linux only."
    )
    TCP_KEEPALIVE_COUNT: int | None = _setting(
        None, "Failed probes before the connection is dropped (TCP_KEEPCNT); Linux only."
    )
    JSON_ERRORS_VERBOSE: bool = _setting(
        False, "Include parser detail in JSON body-parse error responses."
    )
    GRACEFUL_TASK_TIMEOUT: float = _setting(
        10, "Seconds to wait for background tasks to finish cancelling on shutdown."
    )
    MAX_CONCURRENT_CONNECTIONS: int | None = _setting(
        DEFAULT_MAX_CONCURRENT_CONNECTIONS,
        "Cap on simultaneous connections on the built-in serving path; None is unlimited.",
    )
    WRITE_BUFFER_HIGH_WATER: int = _setting(
        DEFAULT_WRITE_BUFFER_HIGH_WATER,
        "Bytes buffered before the built-in server applies write back-pressure.",
    )
    EVENT_LOOP_WATCHDOG: bool | Mapping[str, Any] | None = _setting(
        None, "Truthy enables the event-loop stall watchdog; a mapping tunes it."
    )
    SILENCED_AUDIT_IDS: Sequence[str] = _setting((), "Finding ids security_audit drops.")
    MCP_CALL_TIMEOUT: int | None = _setting(
        None, "Per-call wall-clock budget in seconds for an MCP tool; None is unbounded."
    )
    MCP_ENFORCE_LIFECYCLE: bool = _setting(
        False,
        "Require the MCP initialize handshake before any other request on a stateful connection.",
    )
    MCP_RESOURCE_SUBSCRIPTIONS: bool = _setting(
        False, "Advertise and serve MCP resource subscriptions."
    )
    GRACEFUL_DRAIN_TIMEOUT: float = _setting(
        30, "Seconds shutdown waits for in-flight requests to finish."
    )
    WEBSOCKET_IDLE_TIMEOUT: int | None = _setting(
        None, "Seconds a WebSocket may idle before a 1001 close; None disables it."
    )

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        """Return every declared key with its default value."""
        return dict(_DEFAULTS)

    @classmethod
    def from_config(cls, config: Config) -> Settings:
        """Build a snapshot of `config`, falling back to each field's default."""
        get = config.get
        return cls(**{name: get(name, default) for name, default in _DEFAULTS.items()})


def _read_only(self: Settings, name: str, value: object) -> None:
    raise AttributeError(
        f"`app.settings` is read-only; set `app.config[{name!r}] = ...` instead "
        f"(cannot assign {name})"
    )


def _read_only_delete(self: Settings, name: str) -> None:
    raise AttributeError(f"`app.settings` is read-only; use `app.config` to change {name}")


# Installed after the decorator: a frozen dataclass refuses a `__setattr__`
# written in the class body, and its own message does not name `app.config`.
Settings.__setattr__ = _read_only  # type: ignore[method-assign,assignment]  # frozen dataclasses forbid a body definition
Settings.__delattr__ = _read_only_delete  # type: ignore[method-assign,assignment]  # as above

_DEFAULTS: dict[str, Any] = {
    f.name: f.default for f in fields(Settings) if f.default is not MISSING
}
