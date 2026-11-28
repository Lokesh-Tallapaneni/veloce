"""Plugin subcommands and `custom` argv splitting for the `veloce` CLI.

Third-party packages add subcommands through the `veloce.commands` entry point group;
discovery is lazy, so `veloce --help` never imports plugin code.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import sys
import warnings
from typing import Any

# Each value is a callable taking the subparsers action and adding one parser.
_COMMAND_ENTRY_POINT_GROUP = "veloce.commands"


def _split_custom_argv(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Split a `custom` argv into the argparse head and the Click tail.

    `veloce custom [--env-file PATH | --no-env-file]... app:app [...same flags...] [--] ...args`

    The CLI's own `--env-file` / `--no-env-file` flags are parsed on either
    side of `app`, then everything else is handed to the app's Click group
    verbatim. `argparse.REMAINDER` cannot express this: it begins capturing
    at the first token after `app`, so a trailing `--env-file` would be
    swallowed into the forwarded args and the dotenv file would never load.
    We therefore peel the tail off ourselves - greedily consuming only the
    env-file flags after `app` - and let argparse parse just the head. An
    explicit `--` ends the flag region and is dropped from the forwarded
    args (POSIX convention).

    Returns `(head, tail)` where `tail is None` means "no forwarded args
    region was found" (e.g. the argv is malformed and argparse should emit
    its own usage error against the whole thing).
    """
    if not argv or argv[0] != "custom":
        return argv, None
    # The `app` positional is the first non-flag token after `custom`; a preceding
    # `--env-file PATH` consumes its value, so that token is skipped too.
    idx = 1
    while idx < len(argv) and argv[idx].startswith("-") and argv[idx] != "--":
        if argv[idx] == "--env-file":
            idx += 2
        else:
            idx += 1
    if idx >= len(argv):
        # No `app` token - let argparse report the error.
        return argv, None
    # `head` is ["custom", ..., "app"]; `rest` is the forwarded command.
    head = argv[: idx + 1]
    rest = argv[idx + 1 :]
    # Consume env-file flags that precede the forwarded command.
    cursor = 0
    while cursor < len(rest):
        token = rest[cursor]
        if token == "--no-env-file":
            head.append(token)
            cursor += 1
        elif token == "--env-file":
            head.extend(rest[cursor : cursor + 2])
            cursor += 2
        elif token.startswith("--env-file="):
            head.append(token)
            cursor += 1
        else:
            break
    tail = rest[cursor:]
    if tail and tail[0] == "--":
        tail = tail[1:]
    return head, tail


def _iter_command_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Return the installed `veloce.commands` entry points (best effort).

    `EntryPoints.select(group=...)` is the stable selection API on Python
    3.10+. A broken metadata cache should not take the whole CLI down, so
    any failure to enumerate is swallowed and reported as "no plugins".
    """
    try:
        return list(importlib.metadata.entry_points().select(group=_COMMAND_ENTRY_POINT_GROUP))
    except Exception:  # pragma: no cover - corrupt distribution metadata  # noqa: BLE001 - corrupt distribution metadata must not break the CLI
        return []


def _rollback_subparsers(
    sub: argparse._SubParsersAction[Any],
    *,
    keep: frozenset[str],
) -> None:
    """Remove any subparser whose name is not in `keep`.

    Undoes a partial registration: a plugin that adds one or more parsers
    and then raises (or never sets a `func`) leaves entries in the
    subparsers action's `choices` map and its help-listing actions. This
    deletes every parser added since `keep` was snapshotted so the
    documented "warn and skip" guarantee holds - a failed plugin leaves the
    parser exactly as it was before the plugin ran.
    """
    for name in [n for n in sub.choices if n not in keep]:
        del sub.choices[name]
    sub._choices_actions = [a for a in sub._choices_actions if a.dest in keep]


def _load_plugin_command(
    sub: argparse._SubParsersAction[Any],
    *,
    reserved: frozenset[str],
    name: str,
) -> None:
    """Register the single third-party subcommand named `name`, if any.

    Plugin discovery is deferred until a plugin subcommand is actually
    selected: only the entry point whose name matches `name` is loaded and
    executed, so `veloce`, `veloce --version`, `veloce --help`, and every
    built-in command run without importing or executing any plugin code.

    Each `veloce.commands` entry point loads to a callable that is handed
    the subparsers action and adds exactly one parser (with a `func`
    default) to it. Plugins are isolated from the core: a plugin that fails
    to import, does not load to a callable, raises while registering, leaves
    no `func` default, or whose name collides with a built-in is warned
    about and skipped - and any parser it partially registered is rolled
    back - so the built-in commands always remain usable.
    """
    if name in reserved:
        # A plugin may not shadow a built-in: warn, and do not even load it.
        for ep in _iter_command_entry_points():
            if ep.name == name:
                warnings.warn(
                    f"veloce CLI plugin {name!r} (from {ep.value!r}) collides with an existing "
                    "command; skipping.",
                    stacklevel=2,
                )
        return
    # Sorted by (name, value), so precedence and the collision warning do not
    # depend on install order.
    matches = sorted(
        (ep for ep in _iter_command_entry_points() if ep.name == name),
        key=lambda ep: (ep.name, ep.value),
    )
    if len(matches) > 1:
        losers = ", ".join(repr(ep.value) for ep in matches[1:])
        warnings.warn(
            f"veloce CLI plugin name {name!r} is provided by multiple entry points; using "
            f"{matches[0].value!r} and skipping: {losers}.",
            stacklevel=2,
        )
    for ep in matches[:1]:
        # `BaseException`, so a plugin cannot kill the CLI with `SystemExit`.
        try:
            register = ep.load()
        except BaseException as err:  # noqa: BLE001 - a bad plugin must not break the CLI
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) failed to load: {err!r}; "
                "skipping.",
                stacklevel=2,
            )
            continue
        if not callable(register):
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) is not callable; skipping.",
                stacklevel=2,
            )
            continue
        existing = frozenset(sub.choices)
        try:
            register(sub)
        except BaseException as err:  # noqa: BLE001 - a bad plugin must not break the CLI
            _rollback_subparsers(sub, keep=existing)
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) raised while registering: "
                f"{err!r}; skipping.",
                stacklevel=2,
            )
            continue
        added = sub.choices.get(name)
        registered_func = getattr(added, "_defaults", {}).get("func") if added else None
        if added is None or not callable(registered_func):
            # Without a callable `func` default `main()` would crash: roll back.
            _rollback_subparsers(sub, keep=existing)
            warnings.warn(
                f"veloce CLI plugin {name!r} (from {ep.value!r}) did not register a runnable "
                f"{name!r} command; skipping.",
                stacklevel=2,
            )
            continue
        return


class _VeloceArgumentParser(argparse.ArgumentParser):
    """Top-level parser that special-cases `veloce custom` argv.

    See `_split_custom_argv` for why the forwarded Click args cannot use
    `argparse.REMAINDER` without breaking `--env-file` placed before them.
    """

    def parse_known_args(  # type: ignore[override]  # narrower return than the stdlib stub declares
        self,
        args: list[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        argv = list(sys.argv[1:] if args is None else args)
        head, tail = _split_custom_argv(argv)
        # argparse builds one when given none, so this keeps the return type.
        parsed, extras = super().parse_known_args(head, namespace or argparse.Namespace())
        if tail is not None:
            parsed.cli_args = tail
        return parsed, extras
