"""`Config` dict subclass with convenient loader methods.

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
import json
import os
import types
from collections.abc import Callable, Mapping
from typing import Any


class Config(dict[str, Any]):
    """A dict that knows how to load itself from common config sources.

    Only keys made of ASCII uppercase letters, digits, or underscores
    (and not starting with a digit) are stored — see `_is_uppercase_key`.
    """

    @staticmethod
    def default_config() -> dict[str, Any]:
        """The documented default config keys with their values.

        Seeded into `app.config` at construction so reads never raise
        `KeyError`. Values are the documented defaults; veloce-specific
        behaviour reads several of these (`MAX_CONTENT_LENGTH`,
        `JSON_SORT_KEYS`, `PROPAGATE_EXCEPTIONS`, `TRAP_HTTP_EXCEPTIONS`).
        """
        return {
            "DEBUG": False,
            "TESTING": False,
            "SECRET_KEY": None,
            "SERVER_NAME": None,
            "APPLICATION_ROOT": "/",
            "PREFERRED_URL_SCHEME": "http",
            "MAX_CONTENT_LENGTH": None,
            "MAX_FORM_PARTS": 1000,
            "MAX_FORM_PART_SIZE": 10 * 1024 * 1024,
            "MAX_COOKIE_SIZE": 4093,
            "SESSION_COOKIE_NAME": "session",
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SECURE": False,
            "SESSION_COOKIE_SAMESITE": None,
            "PERMANENT_SESSION_LIFETIME": 2678400,
            "JSON_SORT_KEYS": True,
            "JSONIFY_PRETTYPRINT_REGULAR": False,
            "PROPAGATE_EXCEPTIONS": None,
            "TRAP_HTTP_EXCEPTIONS": False,
            "TRAP_BAD_REQUEST_ERRORS": None,
            "SEND_FILE_MAX_AGE_DEFAULT": None,
        }

    @staticmethod
    def _is_uppercase_key(name: str) -> bool:
        """A valid config key: starts with A-Z, then A-Z/0-9/_."""
        if not name:
            return False
        if not ("A" <= name[0] <= "Z"):
            return False
        return all(c.isupper() or c.isdigit() or c == "_" for c in name)

    # ── from_mapping ─────────────────────────────────────────────────

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

    # ── from_object ──────────────────────────────────────────────────

    def from_object(self, obj: object | str) -> bool:
        """Import UPPERCASE attributes from a module, class, instance, or
        dotted-path string.

        `from_object("myapp.settings.Prod")` resolves the dotted path,
        then walks attributes whose names pass `_is_uppercase_key`.
        """
        if isinstance(obj, str):
            obj = _import_string(obj)
        for name in dir(obj):
            if self._is_uppercase_key(name):
                self[name] = getattr(obj, name)
        return True

    # ── from_pyfile ──────────────────────────────────────────────────

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
        # config file itself propagate — they're legitimate misconfig
        # and silently swallowing them would mask real bugs.
        code = compile(source, filename, "exec")
        exec(code, module.__dict__)
        for name in dir(module):
            if self._is_uppercase_key(name):
                self[name] = getattr(module, name)
        return True

    # ── from_envvar ──────────────────────────────────────────────────

    def from_envvar(self, varname: str, silent: bool = False) -> bool:
        """Read a filename from `os.environ[varname]` and `from_pyfile` it."""
        path = os.environ.get(varname)
        if not path:
            if silent:
                return False
            raise RuntimeError(f"environment variable {varname!r} is not set; cannot load config")
        return self.from_pyfile(path, silent=silent)

    # ── from_prefixed_env ────────────────────────────────────────────

    def from_prefixed_env(
        self,
        prefix: str = "VELOCE",
        loads: Callable[[str], Any] = json.loads,
    ) -> bool:
        """Pull env vars starting with `<prefix>_`, strip the prefix, store
        with JSON-decoded values (falling back to the raw string when JSON
        parsing fails). Nested config via `__` separator: `VELOCE_MAIL__SERVER`
        sets `config["MAIL"]["SERVER"]`.
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
                    self[stripped] = value
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

    # ── from_file ────────────────────────────────────────────────────

    def from_file(
        self,
        filename: str,
        load: Callable[[Any], Mapping[str, Any]] = json.load,
        silent: bool = False,
        text: bool = True,
    ) -> bool:
        """Load any structured file (JSON, TOML via `tomllib.load`, YAML …).

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

    # ── get_namespace ────────────────────────────────────────────────

    def get_namespace(
        self, namespace: str, *, lowercase: bool = True, trim_namespace: bool = True
    ) -> dict[str, Any]:
        """Return all config keys starting with `namespace`, trimmed.

        A helper for extracting one subsystem's settings.
        With `lowercase=True` (default), trimmed keys are lower-cased
        — extension code conventionally uses lowercase attribute names.
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
