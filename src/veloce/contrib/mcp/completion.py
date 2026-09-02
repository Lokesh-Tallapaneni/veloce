"""MCP argument completion — suggest values for a prompt or resource argument.

The Model Context Protocol's ``completion/complete`` lets a client ask the server
to suggest values for one argument of a prompt or a resource template as the user
types. Completion is opt-in per argument: a handler author registers a completer
with ``@app.mcp_completer(prompt=..., argument=...)`` (or ``resource=...``), and
the callable is invoked with the partial value the user has typed plus the values
of the sibling arguments already resolved. A prompt or resource argument with no
registered completer answers with an empty completion - never an error - so a
client may always probe and a server that registers none stays inert.

The completers live on the existing `MCPPrompt` / `MCPResource` descriptors (their
shared `completers` mapping), so completion reuses the prompt and resource
registries rather than maintaining a parallel argument model. `CompletionsCapability`
advertises the ``completions`` capability only when at least one completer is
registered, and answers ``completion/complete`` by resolving the reference,
looking up the argument's completer, and shaping its return into the spec's
``{values, total, hasMore}`` result.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veloce._internal import _is_async_callable, offload
from veloce.contrib.mcp._helpers import _principal_lacks_scopes
from veloce.contrib.mcp.capabilities.base import _ServerCapability
from veloce.contrib.mcp.errors import InvalidParamsError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.prompts import PromptRegistry
    from veloce.contrib.mcp.resources import ResourceRegistry
    from veloce.contrib.mcp.server import MethodHandler

# The MCP completion utility caps a single response at 100 values; a completer
# returning more is truncated to the cap and the overflow is reported via `total`
# and `hasMore` so the client knows further matches exist.
_MAX_COMPLETION_VALUES = 100

# Upper bound on the number of sibling argument values ingested from a client's
# `context.arguments`. The mapping is attacker-supplied and otherwise unbounded, so
# a request carrying more is rejected (an invalid-params error, the same contract
# the module uses for every other malformed completion input) rather than
# materializing an arbitrarily large dict.
_MAX_CONTEXT_ARGS = 1000

# The signature every registered completer satisfies: the partial value the user
# has typed, plus the sibling argument values already resolved. The return is
# `Any` because a completer may be sync or async and may answer with either a
# `CompletionResult` or a bare sequence of strings; `_shape_completion` narrows it.
_Completer = Callable[[str, dict[str, str]], Any]


# ── Completion results ────────────────────────────────────


@dataclass(slots=True)
class CompletionResult:
    """An explicit completion response: candidate values plus optional totals.

    Return this from a completer to declare the full match `total` and whether
    more values exist beyond those returned; return a bare sequence of strings
    instead to let the capability derive both from the values it received.

    Usage::

        @app.mcp_completer(prompt="greet", argument="name")
        async def complete_name(value: str, siblings: dict[str, str]) -> CompletionResult:
            matches = await lookup_names(prefix=value, scope=siblings)
            return CompletionResult(matches[:100], total=len(matches))

    A completer is always called with two arguments - the partial value, and the
    sibling argument values the client has already filled - so it must declare
    both. Declaring only the value raises at call time and surfaces as an
    internal error.
    """

    values: Sequence[str]
    total: int | None = None
    has_more: bool | None = None


def _empty_completion() -> dict[str, Any]:
    """Return the completion result for an argument with no completer."""
    return {"completion": {"values": [], "hasMore": False}}


def _shape_completion(result: Sequence[str] | CompletionResult) -> dict[str, Any]:
    """Shape a completer's return into the MCP ``completion/complete`` result.

    A bare sequence yields its values (capped at 100) with `total`/`hasMore`
    derived from the cap; a `CompletionResult` carries the author's explicit
    `total` / `has_more`, falling back to the derived values when either is unset.
    """
    if isinstance(result, CompletionResult):
        values = list(result.values)
        explicit_total = result.total
        explicit_has_more = result.has_more
    else:
        values = list(result)
        explicit_total = None
        explicit_has_more = None

    capped = values[:_MAX_COMPLETION_VALUES]
    completion: dict[str, Any] = {"values": capped}
    total = explicit_total if explicit_total is not None else len(values)
    completion["total"] = total
    if explicit_has_more is not None:
        completion["hasMore"] = explicit_has_more
    else:
        completion["hasMore"] = total > len(capped)
    return {"completion": completion}


# ── Registration ──────────────────────────────────────────


def attach_completers(app: Any, prompts: PromptRegistry, resources: ResourceRegistry) -> None:
    """Bind every `@app.mcp_completer` registration onto its target descriptor.

    Each registration names a prompt (by name) or a resource (by URI) and one of
    its argument names; the completer is stored in that descriptor's `completers`
    mapping. An unknown target, an unknown argument, or a duplicate completer for
    the same argument raises at mount time so the misconfiguration surfaces before
    a client connects.
    """
    for registration in getattr(app, "_mcp_completers", ()):
        kind, key = registration.kind, registration.key
        argument, completer = registration.argument, registration.completer
        if kind == "prompt":
            prompt = prompts.get(key)
            if prompt is None:
                raise ValueError(
                    f"@app.mcp_completer names prompt {key!r}, which is not a "
                    "registered MCP prompt."
                )
            valid = {arg["name"] for arg in prompt.arguments}
            _attach_one(prompt.completers, key, argument, completer, valid, "prompt")
        else:
            resource = resources.get(key)
            if resource is None:
                raise ValueError(
                    f"@app.mcp_completer names resource {key!r}, which is not a "
                    "registered MCP resource URI."
                )
            valid = set(resource.uri_param_names)
            _attach_one(resource.completers, key, argument, completer, valid, "resource")


def _attach_one(
    store: dict[str, _Completer],
    key: str,
    argument: str,
    completer: _Completer,
    valid_arguments: set[str],
    kind: str,
) -> None:
    """Store one completer under `argument`, validating the argument and uniqueness."""
    if argument not in valid_arguments:
        raise ValueError(
            f"@app.mcp_completer names argument {argument!r} on {kind} {key!r}, "
            f"which has no such argument (its arguments are {sorted(valid_arguments)})."
        )
    if argument in store:
        raise ValueError(f"Duplicate MCP completer for argument {argument!r} on {kind} {key!r}.")
    store[argument] = completer


# ── The capability ────────────────────────────────────────


class CompletionsCapability(_ServerCapability):
    """The ``completion/complete`` method, advertised when a completer exists.

    Completion is opt-in: the capability is advertised only when at least one
    prompt or resource argument carries a registered completer, so a server with
    none stays inert and a client never probes an empty capability.
    """

    __slots__ = ()

    def advertise(self, *, modern: bool = False) -> dict[str, Any] | None:
        if not self._has_completers():
            return None
        return {"completions": {}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {"completion/complete": self._complete}

    def _has_completers(self) -> bool:
        """Return whether any registered prompt or resource carries a completer."""
        server = self._server
        if any(p.completers for p in server.prompts.prompts.values()):
            return True
        return any(r.completers for r in server.resources.resources.values())

    async def _complete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer ``completion/complete`` for one prompt or resource argument.

        The reference selects the primitive (``ref/prompt`` by name, ``ref/resource``
        by URI), the ``argument`` carries the name and the partial value, and an
        optional ``context.arguments`` supplies the sibling values already resolved.
        An argument with no registered completer answers with an empty completion.
        """
        completer, required_scopes, value, context = self._resolve(params)
        if completer is None:
            return _empty_completion()
        if _principal_lacks_scopes(required_scopes):
            # Every other invocation path gates on the owning primitive's
            # scopes; this one ran the completer for anyone. A completer exists
            # to enumerate the legal values of an argument - customer ids,
            # tenant names, file paths - so an under-scoped caller could
            # enumerate the key space of a primitive it may not touch, and
            # narrow it by prefix. Answered as an empty completion rather than
            # an error, matching this module's "a client may always probe"
            # contract: it neither confirms the primitive exists nor names the
            # scopes required.
            return _empty_completion()
        if _is_async_callable(completer):
            result = await completer(value, context)
        else:
            result = await offload(completer, value, context)
        return _shape_completion(result)

    def _resolve(
        self, params: dict[str, Any]
    ) -> tuple[_Completer | None, frozenset[str], str, dict[str, str]]:
        """Resolve a request into its completer, scopes, partial value, and context.

        The completer is `None` when the named argument carries none (the empty
        completion case); a malformed reference or argument is an invalid-params
        error per JSON-RPC.
        """
        ref = params.get("ref")
        if not isinstance(ref, dict):
            raise InvalidParamsError("completion/complete requires a 'ref' object")
        argument = params.get("argument")
        if not isinstance(argument, dict) or not isinstance(argument.get("name"), str):
            raise InvalidParamsError(
                "completion/complete requires an 'argument' object with a string 'name'"
            )
        name = argument["name"]
        value = argument.get("value")
        if not isinstance(value, str):
            raise InvalidParamsError("completion/complete 'argument.value' must be a string")
        context = self._context_arguments(params)

        completers, required_scopes = self._completers_for_ref(ref)
        return completers.get(name), required_scopes, value, context

    def _completers_for_ref(
        self, ref: dict[str, Any]
    ) -> tuple[dict[str, _Completer], frozenset[str]]:
        """Return the completers and required scopes for a ``ref/prompt`` / ``ref/resource``.

        An unknown primitive yields an empty mapping, so the request answers with
        an empty completion rather than an error - the client may always probe.
        """
        ref_type = ref.get("type")
        if ref_type == "ref/prompt":
            prompt_name = ref.get("name")
            if not isinstance(prompt_name, str):
                raise InvalidParamsError("ref/prompt requires a string 'name'")
            prompt = self._server.prompts.get(prompt_name)
            if prompt is None:
                return {}, frozenset()
            return prompt.completers, prompt.tool.required_scopes
        if ref_type == "ref/resource":
            uri = ref.get("uri")
            if not isinstance(uri, str):
                raise InvalidParamsError("ref/resource requires a string 'uri'")
            # A completion reference carries the template URI verbatim (the value
            # `resources/templates/list` advertised), so an exact registry lookup
            # is tried first; a concrete URI then falls back to the pattern match.
            resources = self._server.resources
            resource = resources.get(uri)
            if resource is not None:
                return resource.completers, resource.tool.required_scopes
            matched = resources.match(uri)
            if matched is None:
                return {}, frozenset()
            return matched[0].completers, matched[0].tool.required_scopes
        raise InvalidParamsError(
            "completion/complete 'ref.type' must be ref/prompt or ref/resource"
        )

    @staticmethod
    def _context_arguments(params: dict[str, Any]) -> dict[str, str]:
        """Extract the already-resolved sibling argument values from the request.

        The optional ``context.arguments`` mapping carries the values the client
        has already filled for other arguments, so a completer can narrow its
        suggestions; an absent or malformed context yields an empty mapping.
        """
        context = params.get("context")
        if not isinstance(context, dict):
            return {}
        arguments = context.get("arguments")
        if not isinstance(arguments, dict):
            return {}
        # The client-supplied mapping is otherwise unbounded; reject an oversized
        # one outright rather than materializing every entry.
        if len(arguments) > _MAX_CONTEXT_ARGS:
            raise InvalidParamsError(
                f"completion/complete 'context.arguments' exceeds {_MAX_CONTEXT_ARGS} entries"
            )
        return {k: v for k, v in arguments.items() if isinstance(v, str)}
