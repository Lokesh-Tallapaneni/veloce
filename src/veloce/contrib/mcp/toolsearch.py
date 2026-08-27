"""Serving a large catalogue through search instead of a listing.

Every entry `tools/list` returns lands in the agent's context window. A server
with three hundred tools spends most of that window before the agent has done
anything, and pagination only spreads the same cost over more round trips.

`mount_mcp(tool_search=True)` publishes three tools instead of the catalogue:

- ``search_tools`` ranks the catalogue against a query (BM25 over each tool's
  name, title, description and tags) and returns the best matches.
- ``describe_tools`` returns the full definitions of the tools named, which is
  what the agent needs before it can call one.
- ``run_tools`` runs a list of calls in one request, so a chain of calls costs
  one round trip and only the results the agent asked to keep.

**`run_tools` executes declared calls, never code.** Each step names a
registered tool and its arguments; there is no expression to evaluate and
nothing is compiled, so no sandbox is involved and none is relied on. A step's
argument may reference an earlier step's result - ``{"$from": "step1", "path":
"/items/0/id"}``, an RFC 6901 pointer - which is how a chain passes values along
without routing each intermediate result through the model. Every call runs
through the same path `tools/call` serves: declared scopes, call hooks, the
timeout and the error shaping all apply, and a tool the caller may not invoke is
no more reachable here than it is directly.

Discovery honours the same visibility the listing does, so a `tool_filter` still
decides what a caller can find.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import BaseModel, Field

from veloce._handler_plan import build_plan
from veloce.contrib.mcp._helpers import _text_result
from veloce.contrib.mcp.errors import MCPError, _InvalidArgumentsError
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.contrib.mcp.registry import MCPTool

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

# BM25's saturation and length-normalisation constants. These are the values the
# ranking function is usually reported with, and a tool corpus - a few hundred
# short documents - gives no reason to depart from them.
_K1 = 1.5
_B = 0.75

# A term is a run of letters and digits. Tool names are snake_case or camelCase,
# so both are split into their words before matching.
_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# What `run_tools` calls a step the caller did not name, so a later step can
# still reference it and a result can still be attributed.
_STEP_ID_PREFIX = "step"

_SEARCH_DESCRIPTION = (
    "Find the tools that fit a task. Returns the best matches by name, with a "
    "one-line description each; call describe_tools for a match's full definition "
    "before calling it. This server publishes its catalogue through search rather "
    "than listing it, so this is where every other tool is found."
)

_DESCRIBE_DESCRIPTION = (
    "Return the full definition - description, input schema, annotations - of each "
    "named tool, which is what is needed to call it. Names come from search_tools."
)

_RUN_DESCRIPTION = (
    "Run several tool calls in one request. Each step names a tool and its "
    'arguments; an argument of the form {"$from": "<step id>", "path": "<JSON '
    "pointer>\"} is replaced by that part of an earlier step's result, so a chain "
    "of calls costs one round trip. Set quiet on a step whose result is only "
    "needed by a later step and it is left out of the response. Stops at the first "
    "failing step unless stop_on_error is false."
)


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase terms, breaking camelCase into words."""
    return [chunk.lower() for chunk in _WORD.findall(_CAMEL.sub(" ", text))]


class _Bm25Index:
    """Okapi BM25 over one short document per tool.

    The index is built once, when the server is constructed: a tool's name,
    title, description and tags cannot change after registration, so ranking a
    query is a walk over the query's own terms rather than over the catalogue.
    """

    __slots__ = ("_names", "_postings", "_norms", "_documents")

    def __init__(self, documents: dict[str, str]) -> None:
        self._names = list(documents)
        self._postings: dict[str, dict[int, int]] = {}
        lengths: list[int] = []
        for index, text in enumerate(documents.values()):
            terms = _tokenize(text)
            lengths.append(len(terms))
            for term in terms:
                postings = self._postings.get(term)
                if postings is None:
                    self._postings[term] = postings = {}
                postings[index] = postings.get(index, 0) + 1
        # BM25's length normalisation is fixed once the corpus is - a document's
        # length and the corpus average cannot change after the registry is
        # built - so it is computed here rather than per posting visited. Ranking
        # a query is then arithmetic over the query's own terms.
        average = (sum(lengths) / len(lengths) if lengths else 0.0) or 1.0
        self._norms = [_K1 * (1 - _B + _B * (length or 1) / average) for length in lengths]
        self._documents = len(self._names)

    def search(self, query: str) -> list[tuple[str, float]]:
        """Return `(name, score)` for every tool the query touches, best first."""
        terms = _tokenize(query)
        if not terms or not self._names:
            return []
        documents = self._documents
        norms = self._norms
        scores: dict[int, float] = {}
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            # The probabilistic idf: a term in nearly every document tells the
            # ranking almost nothing, one in a handful tells it a great deal.
            idf = math.log(1 + (documents - len(postings) + 0.5) / (len(postings) + 0.5))
            for index, frequency in postings.items():
                scores[index] = scores.get(index, 0.0) + idf * frequency * (_K1 + 1) / (
                    frequency + norms[index]
                )
        names = self._names
        ranked = sorted(scores.items(), key=lambda item: (-item[1], names[item[0]]))
        return [(names[index], score) for index, score in ranked]


class ToolStep(BaseModel):
    """One call in a `run_tools` plan."""

    tool: str = Field(description="Name of the tool to call.")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments for the tool. A value of the form "
            '{"$from": "<step id>", "path": "<JSON pointer>"} is replaced by that '
            "part of the named earlier step's result."
        ),
    )
    id: str | None = Field(
        default=None,
        description=(
            "Name for this step, so a later step can reference its result. It must "
            "differ from every other step's, including the step<position> name an "
            "unnamed step is given."
        ),
    )
    quiet: bool = Field(
        default=False,
        description=(
            "Leave this step's result out of the response. Later steps can still "
            "reference it, which is how a chain avoids spending context on an "
            "intermediate value."
        ),
    )


def _pointer(value: Any, path: str) -> Any:
    """Return the part of `value` an RFC 6901 JSON pointer names."""
    # RFC 6901 section 3: a pointer is a sequence of zero or more tokens, each
    # prefixed by "/". The empty pointer is the zero-token one and names the whole
    # document; "/" is one token - the empty string - and names the member keyed
    # by "".
    if not path:
        return value
    if not path.startswith("/"):
        raise ValueError(f"a JSON pointer must start with '/': {path!r}")
    for raw in path[1:].split("/"):
        # RFC 6901 section 4 unescapes "~1" before "~0", so an escaped tilde
        # cannot be read as the start of an escaped slash.
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise ValueError(f"{path!r} does not name anything in the referenced result")
            value = value[token]
        elif isinstance(value, list):
            # RFC 6901 section 4: array-index = %x30 / ( %x31-39 *(%x30-39) ) -
            # an unsigned base-10 integer with no leading zero. `isdigit` alone
            # admits non-ASCII digits and `int()` admits a sign, so the ABNF is
            # checked term by term. "-" names the element after the last, which
            # never exists, and falls out here with every other non-index.
            if not token.isdigit() or not token.isascii() or (token[0] == "0" and token != "0"):
                raise ValueError(
                    f"{path!r} indexes an array with {token!r}, which is not an array index"
                )
            index = int(token)
            if index >= len(value):
                raise ValueError(f"{path!r} indexes past the end of the referenced array")
            value = value[index]
        else:
            raise ValueError(f"{path!r} reaches past the end of the referenced result")
    return value


def _resolve_references(node: Any, results: dict[str, Any]) -> Any:
    """Replace every `$from` reference in an argument tree with what it names."""
    if isinstance(node, dict):
        source = node.get("$from")
        if isinstance(source, str) and set(node) <= {"$from", "path"}:
            if source not in results:
                known = sorted(results) or "no earlier step"
                raise ValueError(f"step {source!r} has no result to reference; ran {known}")
            path = node.get("path")
            return _pointer(results[source], path if isinstance(path, str) else "")
        return {key: _resolve_references(value, results) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve_references(item, results) for item in node]
    return node


def _step_ids(steps: list[ToolStep]) -> list[str]:
    """Name every step, refusing a plan that would give one name to two steps.

    A duplicate is refused before the first step runs rather than after: results
    are kept by name, so the second write would silently redirect a reference the
    plan meant for the first, and the response would carry two entries the model
    cannot tell apart. An unnamed step is named for its position and those names
    appear in every response, so they are exactly the names a model reuses.
    """
    named = [step.id or f"{_STEP_ID_PREFIX}{n}" for n, step in enumerate(steps, start=1)]
    # A plan almost never collides, so the common case pays one set build and one
    # length compare; the walk that finds which name repeats runs only when one does.
    if len(set(named)) != len(named):
        seen: set[str] = set()
        for step_id in named:
            if step_id in seen:
                raise _InvalidArgumentsError(
                    f"two steps are named {step_id!r}, so a reference to it would be "
                    "ambiguous; give each step a distinct id, remembering that an "
                    f"unnamed step is named {_STEP_ID_PREFIX}<position>"
                )
            seen.add(step_id)
    return named


def _referenceable(result: dict[str, Any]) -> Any:
    """Return the part of a tool result a later step can point into.

    A tool that declares an output schema answers with `structuredContent`, which
    is the value itself. One that does not answers with text, which is JSON
    whenever the tool returned anything but a string - so it is decoded here
    rather than leaving a pointer to walk a string.
    """
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    # A proxied tool relays its upstream's result object verbatim, so the block
    # is upstream JSON this server never shaped and is not known to be an object.
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
    ):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                return orjson.loads(text)
            except orjson.JSONDecodeError:
                return text
    return content


def _document(tool: MCPTool) -> str:
    """Return the text a tool is ranked on."""
    parts: Iterable[str] = (
        tool.name,
        tool.title or "",
        tool.description or "",
        " ".join(sorted(tool.tags)),
    )
    return " ".join(part for part in parts if part)


def _meta_tool(handler: Any, name: str, description: str) -> MCPTool:
    """Build one of the three tools, deriving its schema from the handler."""
    plan = build_plan(handler)
    schemas: dict[str, dict[str, Any]] = {}
    return MCPTool(
        name=name,
        description=description,
        handler=handler,
        plan=plan,
        input_schema=build_input_schema(plan, schemas),
        annotations={"readOnlyHint": name != "run_tools"},
    )


class ToolSearch:
    """The three tools that stand in for a catalogue too large to list."""

    __slots__ = ("_server", "_index", "tools", "names")

    def __init__(self, server: Any) -> None:
        self._server = server
        self._index = _Bm25Index(
            {tool.name: _document(tool) for tool in server.registry.tools.values()}
        )
        self.tools = [
            _meta_tool(self._search_tools, "search_tools", _SEARCH_DESCRIPTION),
            _meta_tool(self._describe_tools, "describe_tools", _DESCRIBE_DESCRIPTION),
            _meta_tool(self._run_tools, "run_tools", _RUN_DESCRIPTION),
        ]
        self.names = frozenset(tool.name for tool in self.tools)
        taken = self.names & set(server.registry.tools)
        if taken:
            raise ValueError(
                f"tool_search needs the names {sorted(self.names)}, and this server "
                f"already registers {sorted(taken)}; rename those tools or pass "
                "name= on their registration."
            )
        for tool in self.tools:
            server.registry.add(tool)

    async def _reachable(self) -> dict[str, MCPTool]:
        """Return the tools this caller may see, by name.

        The same set `tools/list` reports, so a scope, a visibility policy or a
        name this connection hid narrows discovery exactly as it narrows the
        listing - search stands in for the listing, so it has to answer alike.
        The registry's own map is handed back untouched when nothing can narrow
        it, which is the case `tool_search` exists to serve.
        """
        unnarrowed: dict[str, MCPTool] | None = self._server._unnarrowed_tools()
        if unnarrowed is not None:
            return unnarrowed
        return {tool.name: tool for tool in await self._server._candidate_tools()}

    async def _search_tools(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Rank the catalogue against `query`."""
        reachable = await self._reachable()
        found: list[dict[str, Any]] = []
        for name, score in self._index.search(query):
            tool = reachable.get(name)
            if tool is None or name in self.names:
                continue
            found.append({"name": name, "description": tool.description, "score": round(score, 3)})
            if len(found) >= limit:
                break
        return found

    async def _describe_tools(self, names: list[str]) -> list[dict[str, Any]]:
        """Return the full definition of each named tool."""
        reachable = await self._reachable()
        described: list[dict[str, Any]] = []
        for name in names:
            tool = reachable.get(name)
            if tool is None:
                raise _InvalidArgumentsError(
                    f"no tool named {name!r} is available; search_tools finds one"
                )
            described.append(self._server._describe_tool(tool))
        return described

    async def _run_tools(self, steps: list[ToolStep], stop_on_error: bool = True) -> dict[str, Any]:
        """Run each step in order, passing results along."""
        results: dict[str, Any] = {}
        reported: list[dict[str, Any]] = []
        for step, step_id in zip(steps, _step_ids(steps), strict=True):
            if step.tool in self.names:
                raise _InvalidArgumentsError(f"{step.tool!r} cannot be run as a step of a plan")
            try:
                arguments = _resolve_references(step.arguments, results)
            except ValueError as exc:
                # Surfaced verbatim rather than as a handler failure: the model
                # wrote the reference and the message names what it got wrong.
                raise _InvalidArgumentsError(f"step {step_id!r}: {exc}") from exc
            except RecursionError as exc:
                # An argument tree deeper than the interpreter's stack: `orjson`
                # decodes further than this walk can descend, and the model can act
                # on the depth but not on a message about a Python stack.
                raise _InvalidArgumentsError(
                    f"step {step_id!r}: its arguments nest too deeply to resolve"
                ) from exc
            try:
                result = await self._server._tools_call({"name": step.tool, "arguments": arguments})
            except MCPError as exc:
                # Reported as this step's failure rather than as the whole call's:
                # the steps before it have already run, and a plan that hides the
                # side effects it caused is worse than one that names them.
                result = _text_result(str(exc), is_error=True)
            results[step_id] = _referenceable(result)
            failed = bool(result.get("isError"))
            if not step.quiet or failed:
                reported.append({"id": step_id, "tool": step.tool, "result": result})
            if failed and stop_on_error:
                return {"steps": reported, "stopped": step_id}
        return {"steps": reported}
