"""Config — a `dict` subclass with convenient loader methods.

`app.config` is a `Config`. The class inherits `dict`, so every existing idiom
keeps working. The loader methods pull configuration from modules, mappings,
python files, or env vars, applying the convention that only UPPERCASE keys are
config.
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

_logger = logging.getLogger(__name__)

# Without a cap, a DDoS can open sockets faster than dispatch drains them.
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 1000

# Write-side high watermark (bytes): a producer outrunning a slow client would
# otherwise grow the transport write buffer into a memory-exhaustion vector.
DEFAULT_WRITE_BUFFER_HIGH_WATER = 256 * 1024


def _orjson_load(fp: IO[str] | IO[bytes]) -> Mapping[str, Any]:
    """File-object loader matching `json.load`'s shape, backed by orjson.

    `orjson` only exposes `loads(bytes | str)`, so this keeps
    `Config.from_file(load=...)`'s `Callable[[file], Mapping[str, Any]]` contract.
    """
    return orjson.loads(fp.read())


def _parse_env_lines(lines: list[str], *, source: str = "<env>") -> dict[str, str]:
    """Parse dotenv-style ``KEY=VALUE`` lines into a plain string mapping.

    Full-line `#` comments and blank lines are skipped, an optional `export `
    prefix is accepted, and a value wrapped in matching single or double quotes is
    unquoted. An unquoted value may carry a trailing ` #` inline comment, which is
    stripped; a `#` inside quotes is kept literal. `source` only labels the
    unmatched-quote warning.
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
            # Past the close quote is a comment; inside it, `#` is literal.
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
            # Only a space-delimited `#` is a comment; a bare one is a value.
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        parsed[key] = value
    return parsed


def _import_string(dotted_path: str) -> object:
    """Resolve `"package.module.attr"` to the attribute object.

    Walks right-to-left until a prefix imports cleanly.
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


#: Keys typed but defaulting to `None`, so the type cannot be read off the
#: default. `tests/test_env_file_typing.py` pins this table as complete.
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
    # Only a string needs coercing; a mapping set in code passes through.
    "EVENT_LOOP_WATCHDOG": "bool",
}

#: Keys an env file supplies as free-form text, so the completeness test can
#: tell "deliberately a string" from "nobody decided yet".
_ENV_FREE_FORM: frozenset[str] = frozenset({"SECRET_KEY", "SERVER_NAME", "PREFERRED_URL_SCHEME"})


#: The boolean tokens every dotenv-reading tool has converged on. Spelled out
#: rather than delegated to `pydantic.TypeAdapter`, which would pull
#: `importlib.metadata` onto the base import path and is slower (190ns v 353ns).
_ENV_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
#: `KEY=` reads as "off" for a flag, which an operator can have meant.
_ENV_FALSE = frozenset({"0", "false", "f", "no", "n", "off", ""})

#: What each declared type is called in the error a bad value raises.
_ENV_TYPE_NAMES = {"int": "an integer", "bool": "a boolean (true/false, yes/no, on/off, 1/0)"}


def _coerce_env_typed(value: str, kind: str, *, name: str) -> Any:
    """Parse an env-file string as `kind`, refusing a value that is not one.

    The refusal is the point: without it `DEBUG=flase` is indistinguishable from
    `DEBUG=false`, so a typo in a security flag selects the unsafe value. The
    `ValueError` names the config key, pointing at the `.env` line to fix.
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
    """Build the message for a value that is not the type its key declares."""
    return f"{name} must be {_ENV_TYPE_NAMES[kind]}, got {value!r}"


def _coerce_env_value(key: str, value: Any, current: Any) -> Any:
    """Give an env-file string the type its config key is read as.

    A `.env` file carries no types, so `MAX_CONTENT_LENGTH=1000` would reach a `>`
    against an int and the truthy `DEBUG=false` would read as its own opposite.
    The target type comes from the key's own default; `bool` is tested before
    `int` because it is a subclass of it.
    """
    if not isinstance(value, str):
        return value
    if isinstance(current, bool):
        return _coerce_env_typed(value, "bool", name=key)
    if isinstance(current, int):
        return _coerce_env_typed(value, "int", name=key)
    if isinstance(current, tuple):
        # `A,B` left as a string would match single characters on a `in` test.
        return tuple(part.strip() for part in value.split(",") if part.strip())
    declared = _ENV_TYPED_NONE_DEFAULTS.get(key)
    if declared is not None:
        return _coerce_env_typed(value, declared, name=key)
    return value


_SILENT_MISS = object()


def _read_config_file(
    filename: str,
    mode: str,
    reader: Callable[[Any], Any],
    *,
    silent: bool,
    encoding: str | None = None,
) -> Any:
    """Open `filename`, hand the handle to `reader`, and honour `silent=`.

    Returns `_SILENT_MISS` when the file could not be opened and the caller asked
    for silence. `encoding` stays the caller's choice: `from_env_file` pins utf-8
    while `from_file` leaves a text-mode load on the platform default.
    """
    try:
        with open(filename, mode, encoding=encoding) as handle:
            return reader(handle)
    except OSError:
        if silent:
            return _SILENT_MISS
        raise


class Config(dict[str, Any]):
    """A dict that knows how to load itself from common config sources.

    Only keys made of ASCII uppercase letters, digits, or underscores
    (and not starting with a digit) are stored - see `_is_uppercase_key`.
    """

    @staticmethod
    def default_config() -> dict[str, Any]:
        """Return the documented default config keys with their values.

        Seeded into `app.config` at construction so reads never raise `KeyError`.
        """
        return {
            "DEBUG": False,
            "TESTING": False,
            "SECRET_KEY": None,
            "SERVER_NAME": None,
            "PREFERRED_URL_SCHEME": URL_SCHEME_HTTP,
            # The body is buffered, so unbounded lets one request OOM us.
            "MAX_CONTENT_LENGTH": 100 * 1024 * 1024,
            "MAX_FORM_PARTS": 1000,
            "MAX_FORM_PART_SIZE": 10 * 1024 * 1024,
            "MAX_FORM_FILES": None,
            "MAX_FORM_FIELDS": None,
            "MAX_FORM_FILE_SIZE": None,
            "MAX_FORM_FIELD_SIZE": None,
            "MAX_FORM_FIELD_MEMORY": None,
            # Finding ids the audit drops, so it stays on for everything else.
            "SILENCED_AUDIT_IDS": (),
            "JSON_SORT_KEYS": False,
            "JSONIFY_PRETTYPRINT_REGULAR": False,
            # The verbose reason can leak decoder internals; unset means DEBUG.
            "JSON_ERRORS_VERBOSE": False,
            "PROPAGATE_EXCEPTIONS": None,
            "SEND_FILE_MAX_AGE_DEFAULT": None,
            "REQUEST_HANDLER_TIMEOUT": 30,
            "KEEP_ALIVE_TIMEOUT": 75,
            "REQUEST_TIMEOUT": 30,
            "MAX_PIPELINED_REQUESTS": 64,
            "MAX_CONCURRENT_CONNECTIONS": DEFAULT_MAX_CONCURRENT_CONNECTIONS,
            "WRITE_BUFFER_HIGH_WATER": DEFAULT_WRITE_BUFFER_HIGH_WATER,
            # SO_KEEPALIVE, so the kernel tears down a peer that vanished
            # without a FIN. The knobs map to TCP_KEEPIDLE / INTVL / CNT.
            "TCP_KEEPALIVE": True,
            "TCP_KEEPALIVE_IDLE": None,
            "TCP_KEEPALIVE_INTERVAL": None,
            "TCP_KEEPALIVE_COUNT": None,
            # Truthy turns the watchdog on; a mapping tunes it.
            "EVENT_LOOP_WATCHDOG": None,
            # Declared so an env-file value is typed, not a bare string.
            "MCP_CALL_TIMEOUT": None,
            "MCP_ENFORCE_LIFECYCLE": False,
            "MCP_RESOURCE_SUBSCRIPTIONS": False,
            # Seconds a cancelled `app.spawn(...)` task is awaited on shutdown.
            "GRACEFUL_TASK_TIMEOUT": 10,
            # Seconds shutdown waits for in-flight requests; runs in sequence
            # with the above, so a termination grace period must cover both.
            "GRACEFUL_DRAIN_TIMEOUT": 30,
            # Seconds a WebSocket may idle before a 1001 close, both transports.
            "WEBSOCKET_IDLE_TIMEOUT": None,
        }

    @staticmethod
    def _is_uppercase_key(name: str) -> bool:
        """True when `name` is a valid config key: A-Z, then A-Z/0-9/_."""
        if not name:
            return False
        if not ("A" <= name[0] <= "Z"):
            return False
        # Range check, not `c.isdigit()`, which admits non-ASCII digits.
        return all(("A" <= c <= "Z") or ("0" <= c <= "9") or c == "_" for c in name)

    # ── from_mapping ──────────────────────────────────────

    def from_mapping(self, mapping: Mapping[str, Any] | None = None, **kwargs: Any) -> bool:
        """Bulk-update from `mapping` and/or kwargs.

        Only UPPERCASE keys are config keys. A non-uppercase **keyword** argument
        raises, since `from_mapping(debug=True)` would otherwise leave `DEBUG`
        untouched and say nothing; keys in a `mapping` are filtered quietly, as a
        settings dict legitimately carries entries that are not config.

        Always returns True so the call can be used as a chaining sentinel.
        """
        rejected = sorted(k for k in kwargs if not self._is_uppercase_key(k))
        if rejected:
            raise TypeError(
                f"config keys must be UPPERCASE; got {', '.join(rejected)}. "
                f"Write {rejected[0].upper()}=... , or pass a mapping to have "
                f"non-config keys filtered."
            )
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

        `from_object("myapp.settings.Prod")` resolves the dotted path first.
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

        Returns True on success, or False when `silent=True` and the file is
        missing.
        """
        module = types.ModuleType("veloce_config")
        module.__file__ = filename
        source = _read_config_file(filename, "rb", lambda handle: handle.read(), silent=silent)
        if source is _SILENT_MISS:
            return False
        # Errors raised by the config file itself propagate as real misconfig.
        code = compile(source, filename, "exec")
        exec(code, module.__dict__)
        for name in dir(module):
            if self._is_uppercase_key(name):
                self[name] = getattr(module, name)
        return True

    # ── from_env_file ─────────────────────────────────────

    def from_env_file(self, filename: str = ".env", silent: bool = False) -> bool:
        """Load ``KEY=VALUE`` pairs from a dotenv-style ``.env`` file.

        Full-line `#` comments and blank lines are skipped, an optional `export `
        prefix is accepted, and a value wrapped in matching single or double
        quotes is unquoted. An unquoted value may carry a trailing ` #` inline
        comment, which is stripped; a `#` inside quotes is kept literal. A value
        for a key with a known type is converted to it: `DEBUG=false` stores
        `False` and `MAX_CONTENT_LENGTH=1000` stores `1000`, and an unparseable
        number raises, naming the key. Only UPPERCASE keys are kept (see
        `from_mapping`). With `silent=True` a missing file returns `False` rather
        than raising.

        Keys are stored exactly as the file spells them, and `os.environ` is not
        touched. This does not compose with `from_prefixed_env`, which strips its
        prefix: a file setting `MYAPP_TIMEOUT` becomes the config key
        `MYAPP_TIMEOUT` here and `TIMEOUT` there. Pick one of the two and use it
        on every path.
        """
        lines = _read_config_file(
            filename, "r", lambda handle: handle.readlines(), silent=silent, encoding="utf-8"
        )
        if lines is _SILENT_MISS:
            return False
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
        """Load config from the env vars named `<prefix>_...`, less the prefix.

        Values are JSON-decoded, falling back to the raw string when JSON
        parsing fails. Nested config uses the `__` separator:
        `VELOCE_MAIL__SERVER` sets `config["MAIL"]["SERVER"]`.

        Reads `os.environ` only. `from_env_file` reads a file and keeps each key
        verbatim, so the two name the same setting differently - see its note. A
        value that is not valid JSON is given the type its key is read as, or
        raises by name; a nested key has no declared type and is stored as read.
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
                    # Non-JSON arrives raw, so `..._MAX_CONTENT_LENGTH=10MB`
                    # would meet a `>` against an int on every bodied request.
                    self[stripped] = _coerce_env_value(stripped, value, self.get(stripped))
                continue
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

        Opens in text or binary mode per `text=`, hands the file object to
        `load`, and applies the mapping it returns through `from_mapping`.
        """
        mode = "r" if text else "rb"
        data = _read_config_file(filename, mode, load, silent=silent)
        if data is _SILENT_MISS:
            return False
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

        With `lowercase=True` (default) trimmed keys are lower-cased, matching
        the attribute names extension code conventionally uses.
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
