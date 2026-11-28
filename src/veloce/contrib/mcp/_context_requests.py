"""Server-initiated requests — sampling, elicitation and roots of `MCPContext`."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp._helpers import _text_result
from veloce.contrib.mcp.errors import MCPError
from veloce.contrib.mcp.sampling import _NO_MORE_TOOLS, SampledToolCall, SamplingRun, content_blocks

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer


# A value outside the set is a typo the client would silently drop.
_SAMPLING_CONTEXT_MODES = frozenset({"none", "thisServer", "allServers"})

# A minted `elicitationId` travels to the client, so it is unguessable.
_ELICITATION_ID_ENTROPY_BYTES = 16


def _error_content(message: str) -> dict[str, Any]:
    """Shape a failed sampled tool call the way a tool's own error result is shaped."""
    return _text_result(message, is_error=True)


class _ServerRequestsMixin:
    """The requests the server makes of the client; holds no state of its own."""

    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Provided by `MCPContext`, which this class is mixed into; declared for
        # the checker only, so no runtime stub shadows the real methods.
        async def _request(
            self, method: str, capability: str, params: dict[str, Any]
        ) -> dict[str, Any]: ...

        def _require_server(self, what: str) -> MCPServer: ...

        def _require_sub_capability(self, capability: str, sub: str) -> None: ...

    # ── Server-initiated requests ─────────────────────────────

    async def sample(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        model_preferences: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        include_context: str | None = None,
    ) -> dict[str, Any]:
        """Ask the client's LLM to sample a completion (sampling/createMessage).

        Returns the client's result (its chosen model, role, and content). Requires
        a bidirectional transport and a client that advertised the ``sampling``
        capability; `tools` / `tool_choice` additionally require ``sampling.tools``.

        `include_context` asks the client to attach context from MCP servers to the
        prompt - ``"none"``, ``"thisServer"``, or ``"allServers"``. The client MAY
        ignore the request, so it is a hint rather than a guarantee.

        Args:
            messages: Conversation so far, as MCP sampling messages.
            max_tokens: Upper bound on the completion length, in tokens.
            model_preferences: Optional hints for the client's model choice.
            system_prompt: Optional system prompt the client may apply.
            temperature: Optional sampling temperature.
            stop_sequences: Optional sequences that end the completion.
            tools: Tool definitions the model may call; needs `sampling.tools` on the client.
            tool_choice: How the model may use `tools`; needs `sampling.tools` on the client.
            metadata: Optional provider-specific metadata, passed through.
            include_context: `none`, `thisServer` or `allServers`.

        Returns:
            The client's sampling result: its chosen model, the role and the content.

        Raises:
            ValueError: If `include_context` is not one of the accepted modes.
            RuntimeError: If the transport cannot carry server-initiated requests.
            MCPCapabilityError: If the client did not advertise `sampling`, or `sampling.tools`
                when tools are given.

        """  # noqa: DOC503 - the exception is raised or caught by a callee
        if include_context is not None and include_context not in _SAMPLING_CONTEXT_MODES:
            raise ValueError(
                f"include_context must be one of {sorted(_SAMPLING_CONTEXT_MODES)}, "
                f"got {include_context!r}"
            )
        params: dict[str, Any] = {"messages": messages, "maxTokens": max_tokens}
        if model_preferences is not None:
            params["modelPreferences"] = model_preferences
        if system_prompt is not None:
            params["systemPrompt"] = system_prompt
        if temperature is not None:
            params["temperature"] = temperature
        if stop_sequences is not None:
            params["stopSequences"] = stop_sequences
        if tools is not None or tool_choice is not None:
            self._require_sub_capability("sampling", "tools")
            if tools is not None:
                params["tools"] = tools
            if tool_choice is not None:
                params["toolChoice"] = tool_choice
        if metadata is not None:
            params["metadata"] = metadata
        if include_context is not None:
            params["includeContext"] = include_context
        return await self._request("sampling/createMessage", "sampling", params)

    async def sample_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[str],
        max_tokens: int,
        max_tool_rounds: int = 5,
        model_preferences: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SamplingRun:
        """Sample with tools, executing the ones the model asks for, until it answers.

        `tools` names tools of this server the model may drive. Each request the
        model makes runs through the same path `tools/call` serves - declared
        scopes, call hooks, timeout and error shaping included - and its result
        is fed back as the next message. Returns a `SamplingRun` carrying the
        answer, the transcript, and every tool call made.

        `max_tool_rounds` caps how many times tools are executed; on the round
        after the cap the model is asked to answer without tools.

        Requires a client that advertised ``sampling.tools``.

        Args:
            messages: Conversation so far, as MCP sampling messages.
            tools: Names of this server's tools the model may drive.
            max_tokens: Upper bound on each completion's length, in tokens.
            max_tool_rounds: How many times tools may run before the model must answer.
            model_preferences: Optional hints for the client's model choice.
            system_prompt: Optional system prompt the client may apply.
            temperature: Optional sampling temperature.
            stop_sequences: Optional sequences that end a completion.
            tool_choice: How the model may use the tools on the first rounds.
            metadata: Optional provider-specific metadata, passed through.

        Returns:
            The `SamplingRun`: the final answer, the transcript and every tool call made.

        Raises:
            RuntimeError: If this context has no serving `MCPServer`, or the transport cannot
                carry server-initiated requests.
            ValueError: If `tools` names a tool this server does not register.
            MCPCapabilityError: If the client did not advertise `sampling.tools`.

        """  # noqa: DOC503 - the exception is raised or caught by a callee
        server = self._require_server("sample_with_tools")
        declared = self._declare_tools(server, tools)
        allowed = frozenset(tools)

        transcript = list(messages)
        calls: list[SampledToolCall] = []
        for round_index in range(max_tool_rounds + 1):
            final_round = round_index == max_tool_rounds
            # A copy per round: the request holds it while the loop appends.
            result = await self.sample(
                list(transcript),
                max_tokens=max_tokens,
                model_preferences=model_preferences,
                system_prompt=system_prompt,
                temperature=temperature,
                stop_sequences=stop_sequences,
                tools=declared,
                tool_choice=_NO_MORE_TOOLS if final_round else tool_choice,
                metadata=metadata,
            )
            blocks = content_blocks(result)
            requested = [block for block in blocks if block.get("type") == "tool_use"]
            if not requested or final_round:
                # The answer closes the transcript, so `messages` can extend
                # into another run.
                transcript.append({"role": "assistant", "content": blocks})
                return SamplingRun(
                    content=tuple(blocks),
                    model=result.get("model"),
                    stop_reason=result.get("stopReason"),
                    messages=tuple(transcript),
                    tool_calls=tuple(calls),
                    rounds=round_index + 1,
                )
            transcript.append({"role": "assistant", "content": blocks})
            results = []
            for block in requested:
                call = await self._run_sampled_tool(server, block, allowed)
                calls.append(call)
                answer: dict[str, Any] = {
                    "type": "tool_result",
                    "toolUseId": call.id,
                    "content": call.result.get("content") or [],
                    "isError": call.is_error,
                }
                # So the model sees the shape a direct `tools/call` caller gets.
                structured = call.result.get("structuredContent")
                if structured is not None:
                    answer["structuredContent"] = structured
                results.append(answer)
            transcript.append({"role": "user", "content": results})
        raise AssertionError("unreachable: the final round always returns")

    def _declare_tools(self, server: MCPServer, names: list[str]) -> list[dict[str, Any]]:
        """Return the `tools/list` entries for the named tools of this server."""
        declared: list[dict[str, Any]] = []
        for name in names:
            tool = server.registry.get(name)
            if tool is None:
                raise ValueError(
                    f"sample_with_tools names {name!r}, which this server does not "
                    f"register; it has {sorted(server.registry.tools) or 'no tools'}"
                )
            declared.append(server._describe_tool(tool))
        return declared

    async def _run_sampled_tool(
        self, server: MCPServer, block: dict[str, Any], allowed: frozenset[str]
    ) -> SampledToolCall:
        """Execute one tool the model asked for, as `tools/call` would.

        A failure comes back as an error result rather than propagating, so the
        model can correct an argument it generated badly.
        """
        name = block.get("name")
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            arguments = {}
        use_id = str(block.get("id") or "")
        if not isinstance(name, str) or name not in allowed:
            return SampledToolCall(
                name=str(name),
                id=use_id,
                arguments=arguments,
                result=_error_content(f"{name!r} is not one of the tools offered for this run"),
                is_error=True,
            )
        try:
            result = await server._tools_call({"name": name, "arguments": arguments})
        except MCPError as exc:
            return SampledToolCall(
                name=name,
                id=use_id,
                arguments=arguments,
                result=_error_content(str(exc)),
                is_error=True,
            )
        return SampledToolCall(
            name=name,
            id=use_id,
            arguments=arguments,
            result=result,
            is_error=bool(result.get("isError")),
        )

    async def elicit(
        self,
        message: str,
        *,
        requested_schema: dict[str, Any] | None = None,
        url: str | None = None,
        elicitation_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the client to gather input from its user (elicitation/create).

        Form mode passes a `requested_schema` (the JSON Schema of the fields to
        collect); URL mode passes a `url` the client opens instead. Returns the
        client's response (its action and any collected content). Requires a
        bidirectional transport and a client that advertised ``elicitation``.

        URL mode also carries the `elicitationId` the spec requires, which names
        the interaction in a later `notifications/elicitation/complete`. One is
        minted per call; pass `elicitation_id` to reuse the URL flow's own.

        Args:
            message: Prompt shown to the user.
            requested_schema: JSON Schema of the fields to collect (form mode).
            url: URL the client opens instead of showing a form (URL mode).
            elicitation_id: Identifier for the URL flow; one is generated when omitted.

        Returns:
            The client's response: the action taken and any collected content.

        Raises:
            ValueError: If both `requested_schema` and `url` are given.
            RuntimeError: If the transport cannot carry server-initiated requests.
            MCPCapabilityError: If the client did not advertise `elicitation` (or its `url`
                mode, for URL elicitation).

        """  # noqa: DOC503 - the exception is raised or caught by a callee
        if requested_schema is not None and url is not None:
            raise ValueError("elicit takes either requested_schema (form) or url, not both")
        params: dict[str, Any] = {"message": message}
        if url is not None:
            # The spec forbids sending a mode the client did not declare, and an
            # empty `elicitation: {}` means form-only, so only URL mode is gated.
            self._require_sub_capability("elicitation", "url")
            params["mode"] = "url"
            params["url"] = url
            params["elicitationId"] = elicitation_id or secrets.token_urlsafe(
                _ELICITATION_ID_ENTROPY_BYTES
            )
        elif requested_schema is not None:
            params["requestedSchema"] = requested_schema
        return await self._request("elicitation/create", "elicitation", params)

    async def roots(self) -> list[dict[str, Any]]:
        """List the client's exposed filesystem roots (roots/list).

        Returns the client's `roots` array. Requires a bidirectional transport and
        a client that advertised the ``roots`` capability.
        """
        result = await self._request("roots/list", "roots", {})
        roots = result.get("roots")
        return roots if isinstance(roots, list) else []
