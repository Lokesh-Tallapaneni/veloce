"""Config — a `dict` subclass with convenient loader methods.

`app.config` is a `Config`. The class inherits `dict`, so every existing
idiom (`config["DEBUG"] = True`, `config.get("X")`, `config.update(...)`)
keeps working. The loader methods below pull configuration from
modules, mappings, python files, or env vars, applying the
convention that only UPPERCASE keys are config (lowercase names are
private to the source).

Public method names follow widely-used config-loader conventions;
only the observable behaviour is modelled.
"""

from __future__ import annotations

import importlib
import logging
import os
import types
from collections.abc import Callable, Mapping
from typing import IO, Any

import orjson

from veloce._protocol_constants import URL_SCHEME_HTTP

# Per-process cap on simultaneously-open connections for the built-in serving
# path. Without it, a DDoS can exhaust RAM by opening sockets faster than
# dispatch can drain them.
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 1000

# Write-side flow-control high watermark (bytes). When a streaming/SSE producer
# outruns a slow client the event loop's transport write buffer grows; left
# unbounded that is a per-connection memory-exhaustion vector. Handed to
# `transport.set_write_buffer_limits()`, it makes asyncio invoke
# `pause_writing`/`resume_writing` once the buffer crosses the mark, which the
# streaming path awaits on. The low mark is left to asyncio's default (a
# quarter of high) when only the high mark is supplied.
DEFAULT_WRITE_BUFFER_HIGH_WATER = 256 * 1024

_logger = logging.getLogger(__name__)


def _orjson_load(fp: IO[str] | IO[bytes]) -> Mapping[str, Any]:
    """File-object loader matching `json.load`'s shape, backed by orjson.

    `orjson` only exposes `loads(bytes | str)`; this thin adaptor reads
    the file once and delegates so `Config.from_file(load=...)` keeps
    its `Callable[[file], Mapping[str, Any]]` contract.
    """
    return orjson.loads(fp.read())


def _parse_env_lines(lines: list[str], *, source: str = "<env>") -> dict[str, str]:
    """Parse dotenv-style ``KEY=VALUE`` lines into a plain string mapping.

    Full-line `#` comments and blank lines are skipped, an optional
    `export ` prefix is accepted, and a value wrapped in matching single
    or double quotes is unquoted. An unquoted value may carry a trailing
    ` #` inline comment, which is stripped; a `#` inside quotes is kept
    literal. `source` only labels the unmatched-quote warning. Shared by
    `Config.from_env_file` and the CLI `--env-file` loader so both paths
    parse identically.
    """
    parsed: dict[str, str] = {}
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted value - take the span up to the matching close
            # quote. Anything after it is an inline comment and is
            # dropped; a `#` *inside* the quotes stays literal.
            quote = value[0]
            close = value.find(quote, 1)
            if close == -1:
                _logger.warning(
                    "env file %s line %d: key %r has unmatched %s quote; "
                    "treating remainder of line as the value",
                    source,
                    lineno,
                    key,
                    quote,
                )
                value = value[1:]
            else:
                value = value[1:close]
        else:
            # Unquoted value - a whitespace-delimited `#` starts an
            # inline comment. A bare `#` (no leading space) is kept,
            # since it may be a legitimate part of the value.
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        parsed[key] = value
    return parsed


def _import_string(dotted_path: str) -> object:
    """Resolve `"package.module.attr"` to the attribute object.

    Walks right-to-left until a prefix imports cleanly, then takes
    the remaining segments as attribute accesses.
    """
    parts = dotted_path.split(".")
    for split_at in range(len(parts), 0, -1):
        module_path = ".".join(parts[:split_at])
        attrs = parts[split_at:]
        try:
            obj: Any = importlib.import_module(module_path)
        except ImportError:
            continue
        try:
            for attr in attrs:
                obj = getattr(obj, attr)
        except AttributeError as err:
            raise ImportError(
                f"module {module_path!r} has no attribute path {'.'.join(attrs)!r}"
            ) from err
        return obj
    raise ImportError(f"could not import {dotted_path!r}")


#: Keys whose value is typed but whose default is `None`, so the type cannot be
#: read off the default. Every other key is typed from its own default, and
#: `tests/test_env_file_typing.py` asserts this table plus the typed defaults
#: covers every key in `default_config()` - a new key cannot be added without a
#: decision about what an env file's string should become.
_ENV_TYPED_NONE_DEFAULTS: dict[str, str] = {
    "MAX_FORM_FIELDS": "int",
    "MAX_FORM_FIELD_MEMORY": "int",
    "MAX_FORM_FIELD_SIZE": "int",
    "MAX_FORM_FILES": "int",
    "MAX_FORM_FILE_SIZE": "int",
    "PROPAGATE_EXCEPTIONS": "bool",
    "SEND_FILE_MAX_AGE_DEFAULT": "int",
    "TCP_KEEPALIVE_COUNT": "int",
    "TCP_KEEPALIVE_IDLE": "int",
    "TCP_KEEPALIVE_INTERVAL": "int",
    "WEBSOCKET_IDLE_TIMEOUT": "int",
    "MCP_CALL_TIMEOUT": "int",
    # Truthy enables the watchdog; a mapping tunes it. Only a string needs
    # coercing - a mapping set in code passes through untouched.
    "EVENT_LOOP_WATCHDOG": "bool",
}

#: Keys an env file supplies as free-form text. Listed so the completeness test
#: can tell "deliberately a string" from "nobody decided yet".
_ENV_FREE_FORM: frozenset[str] = frozenset({"SECRET_KEY", "SERVER_NAME", "PREFERRED_URL_SCHEME"})


#: The tokens an env file may write for a boolean, matching what Pydantic's own
#: bool parser accepts - the same set every dotenv-reading tool has converged on.
#: Written out here rather than delegated to `pydantic.TypeAdapter`: importing
#: `TypeAdapter` pulls `importlib.metadata` onto the base import path, which
#: `test_import_laziness` forbids for cold-start reasons, and the membership test
#: is about twice as fast besides (190ns against 353ns, measured).
_ENV_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
#: The empty string is here and not an error: every dotenv reader treats `KEY=`
#: as an empty value, and for a flag the conventional reading is "off". It is a
#: value the operator can have meant; `flase` is not.
_ENV_FALSE = frozenset({"0", "false", "f", "no", "n", "off", ""})

#: What each declared type is called in the error a bad value raises.
_ENV_TYPE_NAMES = {"int": "an integer", "bool": "a boolean (true/false, yes/no, on/off, 1/0)"}


def _coerce_env_typed(value: str, kind: str, *, name: str) -> Any:
    """Parse an env-file string as `kind`, refusing a value that is not one.

    The refusal is the point. The integer path always rejected a non-integer, but
    the boolean path could not fail: anything outside the truthy tokens read as
    `False`, so `DEBUG=flase` was indistinguishable from `DEBUG=false` and a typo
    in a security flag silently selected the unsafe value.

    Raises `ValueError` naming the config key, so the message points at the line
    of the `.env` file to fix rather than at a `TypeError` several layers later.
    """
    if kind == "bool":
        token = value.strip().lower()
        if token in _ENV_TRUE:
            return True
        if token in _ENV_FALSE:
            return False
        raise ValueError(_env_type_error(name, kind, value))
    try:
        return int(value)
    except ValueError as err:
        raise ValueError(_env_type_error(name, kind, value)) from err


def _env_type_error(name: str, kind: str, value: str) -> str:
    """The message for a value that is not the type its key declares."""
    return f"{name} must be {_ENV_TYPE_NAMES[kind]}, got {value!r}"


def _coerce_env_value(key: str, value: Any, current: Any) -> Any:
    """Give an env-file string the type its config key is read as.

    A `.env` file carries no types, so every value arrives as a string and
    `MAX_CONTENT_LENGTH=1000` reached a `>` against an int - a `TypeError` on
    every request carrying a body. `DEBUG=false` was worse: a non-empty string
    is truthy, so the setting read as the opposite of what was written.

    The target type comes from the key's own default, which is the one place
    already describing what the key holds. `bool` is tested before `int`
    because `bool` is a subclass of it.
    """
    if not isinstance(value, str):
        return value
    if isinstance(current, bool):
        return _coerce_env_typed(value, "bool", name=key)
    if isinstance(current, int):
        return _coerce_env_typed(value, "int", name=key)
    if isinstance(current, tuple):
        # A list-valued key is written `A,B` in an env file; left a string, a
        # membership test would match single characters rather than entries.
        return tuple(part.strip() for part in value.split(",") if part.strip())
    declared = _ENV_TYPED_NONE_DEFAULTS.get(key)
    if declared is not None:
        return _coerce_env_typed(value, declared, name=key)
    return value


class Config(dict[str, Any]):
    """A dict that knows how to load itself from common config sources.

    Only keys made of ASCII uppercase letters, digits, or underscores
    (and not starting with a digit) are stored - see `_is_uppercase_key`.
    """

    @staticmethod
    def default_config() -> dict[str, Any]:
        """The documented default config keys with their values.

        Seeded into `app.config` at construction so reads never raise
        `KeyError`. Values are the documented defaults; veloce-specific
        behaviour reads several of these (`MAX_CONTENT_LENGTH`,
        `JSON_SORT_KEYS`, `PROPAGATE_EXCEPTIONS`).
        """
        return {
            "DEBUG": False,
            "TESTING": False,
            "SECRET_KEY": None,
            "SERVER_NAME": None,
            "PREFERRED_URL_SCHEME": URL_SCHEME_HTTP,
            # Default request-body ceiling. The body is buffered in memory, so an
            # unbounded default lets one large request OOM the process; 100 MiB is
            # generous for typical uploads while bounding that exposure. Set a
            # larger value (or `None` for unlimited) for large-upload endpoints.
            "MAX_CONTENT_LENGTH": 100 * 1024 * 1024,
            "MAX_FORM_PARTS": 1000,
            "MAX_FORM_PART_SIZE": 10 * 1024 * 1024,
            "MAX_FORM_FILES": None,
            "MAX_FORM_FIELDS": None,
            "MAX_FORM_FILE_SIZE": None,
            "MAX_FORM_FIELD_SIZE": None,
            "MAX_FORM_FIELD_MEMORY": None,
            # Finding ids the audit drops. An accepted finding is turned off
            # by id so the audit stays on for everything else.
            "SILENCED_AUDIT_IDS": (),
            "JSON_SORT_KEYS": False,
            "JSONIFY_PRETTYPRINT_REGULAR": False,
            # Surface the verbose JSON decoder reason in the 400 response body.
            # Off in production so a malformed body can't leak decoder internals;
            # falls back to DEBUG when this key is unset.
            "JSON_ERRORS_VERBOSE": False,
            "PROPAGATE_EXCEPTIONS": None,
            "SEND_FILE_MAX_AGE_DEFAULT": None,
            "REQUEST_HANDLER_TIMEOUT": 30,
            "KEEP_ALIVE_TIMEOUT": 75,
            "REQUEST_TIMEOUT": 30,
            # Read by the built-in serving path alongside the timeouts above;
            # seeded here so every key it consults is discoverable in one place.
            "MAX_CONCURRENT_CONNECTIONS": DEFAULT_MAX_CONCURRENT_CONNECTIONS,
            "WRITE_BUFFER_HIGH_WATER": DEFAULT_WRITE_BUFFER_HIGH_WATER,
            # OS-level TCP keepalive for the built-in (Veloce.run / gunicorn
            # worker) serving path. When enabled, SO_KEEPALIVE is set on each
            # accepted socket so the kernel probes idle peers and tears down
            # half-open connections the application-level idle timer would never
            # see (a peer that vanished without a FIN). The idle/interval/count
            # knobs map to TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT and are
            # applied only on platforms that expose them; left None they keep
            # the OS defaults. Has no effect under ASGI servers, which own their
            # own sockets.
            "TCP_KEEPALIVE": True,
            "TCP_KEEPALIVE_IDLE": None,
            "TCP_KEEPALIVE_INTERVAL": None,
            "TCP_KEEPALIVE_COUNT": None,
            # Per-task budget, in seconds, for draining an `app.spawn(...)`
            # background task on shutdown: each task is cancelled and awaited
            # for at most this long before the drain moves on.
            # Read by `app/lifecycle.py`. Truthy turns the event-loop watchdog
            # on; a mapping additionally tunes it (`interval`,
            # `stall_threshold`).
            "EVENT_LOOP_WATCHDOG": None,
            # Read by `contrib/mcp/server.py`. Declared here so they carry a
            # documented default and an env-file value gets the right type -
            # unregistered, `MCP_CALL_TIMEOUT=5` reached `asyncio.wait_for` as a
            # string and broke every tool call.
            "MCP_CALL_TIMEOUT": None,
            "MCP_ENFORCE_LIFECYCLE": False,
            "MCP_RESOURCE_SUBSCRIPTIONS": False,
            "GRACEFUL_TASK_TIMEOUT": 10,
            # How long shutdown waits for in-flight requests to finish after
            # every connection has been asked to quiesce. Separate from
            # GRACEFUL_TASK_TIMEOUT, which bounds background-task cancellation:
            # the two run in sequence, so a container's termination grace period
            # must cover both.
            "GRACEFUL_DRAIN_TIMEOUT": 30,
            # Seconds a WebSocket may sit idle before it is closed 1001.
            # `None` disables it. Applies on both transports.
            "WEBSOCKET_IDLE_TIMEOUT": None,
        }

    @staticmethod
    def _is_uppercase_key(name: str) -> bool:
        """A valid config key: starts with A-Z, then A-Z/0-9/_."""
        if not name:
            return False
        if not ("A" <= name[0] <= "Z"):
            return False
        # ASCII-digit range check, not `c.isdigit()`: the latter also returns
        # True for non-ASCII digit characters (superscripts, Arabic-Indic, ...),
        # which would admit keys the documented ASCII contract forbids.
        return all(("A" <= c <= "Z") or ("0" <= c <= "9") or c == "_" for c in name)

    # ── from_mapping ──────────────────────────────────────

    def from_mapping(self, mapping: Mapping[str, Any] | None = None, **kwargs: Any) -> bool:
        """Bulk-update from `mapping` and/or kwargs.

        Only UPPERCASE keys are stored; lowercase keys are silently
        skipped. Always returns True so the call can be used as a
        chaining sentinel.
        """
        merged: dict[str, Any] = {}
        if mapping is not None:
            merged.update(mapping)
        merged.update(kwargs)
        for k, v in merged.items():
            if self._is_uppercase_key(k):
                self[k] = v
        return True

    # ── from_object ───────────────────────────────────────

    def from_object(self, obj: object | str) -> bool:
        """Import UPPERCASE attributes from a module, class, instance, or dotted-path string.

        `from_object("myapp.settings.Prod")` resolves the dotted path,
        then walks attributes whose names pass `_is_uppercase_key`.
        """
        if isinstance(obj, str):
            obj = _import_string(obj)
        for name in dir(obj):
            if self._is_uppercase_key(name):
                self[name] = getattr(obj, name)
        return True

    # ── from_pyfile ───────────────────────────────────────

    def from_pyfile(self, filename: str, silent: bool = False) -> bool:
        """Execute a Python file and pull UPPERCASE module-level names.

        Returns True on success. If `silent=True` and the file is
        missing, returns False instead of raising.
        """
        module = types.ModuleType("veloce_config")
        module.__file__ = filename
        try:
            with open(filename, "rb") as f:
                source = f.read()
        except OSError:
            if silent:
                return False
            raise
        # Compile + exec into the module namespace. Errors raised by the
        # config file itself propagate - they're legitimate misconfig
        # and silently swallowing them would mask real bugs.
        code = compile(source, filename, "exec")
        exec(code, module.__dict__)
        for name in dir(module):
            if self._is_uppercase_key(name):
                self[name] = getattr(module, name)
        return True

    # ── from_env_file ─────────────────────────────────────

    def from_env_file(self, filename: str = ".env", silent: bool = False) -> bool:
        """Load ``KEY=VALUE`` pairs from a dotenv-style ``.env`` file.

        Full-line `#` comments and blank lines are skipped, an optional
        `export ` prefix is accepted, and a value wrapped in matching
        single or double quotes is unquoted. An unquoted value may carry
        a trailing ` #` inline comment, which is stripped; a `#` inside
        quotes is kept literal. A `.env` file carries no types, so a value for
        a key with a known type is converted to it: `DEBUG=false` stores `False`
        rather than a truthy string, and `MAX_CONTENT_LENGTH=1000` stores `1000`
        rather than `"1000"`. An unparseable number raises, naming the key. Only
        UPPERCASE keys are kept (see `from_mapping`). With `silent=True` a missing file returns
        `False` rather than raising.

        Keys are stored exactly as the file spells them, and `os.environ` is
        not touched. This does not compose with `from_prefixed_env`, which
        strips its prefix: a file setting `MYAPP_TIMEOUT` becomes the config key
        `MYAPP_TIMEOUT` here and `TIMEOUT` there. `veloce run` seeds
        `os.environ` from the same file before importing the app, so an app
        using both reads two different keys depending on how it was started -
        pick one of the two and use it on every path.
        """
        try:
            with open(filename, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            if silent:
                return False
            raise
        parsed = _parse_env_lines(lines, source=filename)
        defaults = self.default_config()
        typed = {
            key: _coerce_env_value(key, value, self.get(key, defaults.get(key)))
            for key, value in parsed.items()
        }
        return self.from_mapping(typed)

    # ── from_envvar ───────────────────────────────────────

    def from_envvar(self, varname: str, silent: bool = False) -> bool:
        """Read a filename from `os.environ[varname]` and `from_pyfile` it."""
        path = os.environ.get(varname)
        if not path:
            if silent:
                return False
            raise RuntimeError(f"environment variable {varname!r} is not set; cannot load config")
        return self.from_pyfile(path, silent=silent)

    # ── from_prefixed_env ─────────────────────────────────

    def from_prefixed_env(
        self,
        prefix: str = "VELOCE",
        loads: Callable[[str], Any] = orjson.loads,
    ) -> bool:
        """Pull env vars starting with `<prefix>_`, strip the prefix, store
        with JSON-decoded values (falling back to the raw string when JSON
        parsing fails). Nested config via `__` separator: `VELOCE_MAIL__SERVER`
        sets `config["MAIL"]["SERVER"]`.

        Reads `os.environ` only. `from_env_file` reads a file and keeps each key
        verbatim, so the two name the same setting differently - see its note.

        A value that is not valid JSON is given the type its key is read as, the
        same way the file loader does; a value that cannot be converted raises,
        naming the key. Nested keys have no declared type and are stored as read.
        """
        sep = f"{prefix}_"
        for name, raw in os.environ.items():
            if not name.startswith(sep):
                continue
            stripped = name[len(sep) :]
            try:
                value: Any = loads(raw)
            except (ValueError, TypeError):
                value = raw
            if "__" not in stripped:
                if self._is_uppercase_key(stripped):
                    # A value that is not valid JSON arrives here as the raw
                    # string, so `VELOCE_MAX_CONTENT_LENGTH=10MB` would be stored
                    # as `str` and then compared with `>` against an int on every
                    # request carrying a body. The same coercion the file loader
                    # applies gives it the type its key is read as, or refuses it
                    # by name at load time instead of failing per request.
                    self[stripped] = _coerce_env_value(stripped, value, self.get(stripped))
                continue
            # Nested: walk segments and set the leaf.
            segments = stripped.split("__")
            if not all(self._is_uppercase_key(s) for s in segments):
                continue
            cursor: dict[str, Any] = self
            for seg in segments[:-1]:
                next_node = cursor.get(seg)
                if not isinstance(next_node, dict):
                    next_node = {}
                    cursor[seg] = next_node
                cursor = next_node
            cursor[segments[-1]] = value
        return True

    # ── from_file ─────────────────────────────────────────

    def from_file(
        self,
        filename: str,
        load: Callable[[Any], Mapping[str, Any]] = _orjson_load,
        silent: bool = False,
        text: bool = False,
    ) -> bool:
        """Load any structured file (JSON, TOML via `tomllib.load`, YAML ...).

        Opens the file in text or binary mode (per `text=`), hands the
        file object to `load`, expects a mapping back, then applies it
        through `from_mapping`.
        """
        mode = "r" if text else "rb"
        try:
            with open(filename, mode) as f:
                data = load(f)
        except OSError:
            if silent:
                return False
            raise
        if not isinstance(data, Mapping):
            raise TypeError(
                f"config loader {load!r} returned {type(data).__name__}, expected a mapping"
            )
        return self.from_mapping(data)

    # ── get_namespace ─────────────────────────────────────

    def get_namespace(
        self, namespace: str, *, lowercase: bool = True, trim_namespace: bool = True
    ) -> dict[str, Any]:
        """Return all config keys starting with `namespace`, trimmed.

        A helper for extracting one subsystem's settings.
        With `lowercase=True` (default), trimmed keys are lower-cased
        - extension code conventionally uses lowercase attribute names.
        """
        result: dict[str, Any] = {}
        for key, value in self.items():
            if not key.startswith(namespace):
                continue
            trimmed = key[len(namespace) :] if trim_namespace else key
            if lowercase:
                trimmed = trimmed.lower()
            result[trimmed] = value
        return result
