"""Centralized type definitions for Veloce framework."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from typing import Any

# ASGI types
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Handler types
SyncHandler = Callable[..., Any]
AsyncHandler = Callable[..., Coroutine[Any, Any, Any]]
Handler = SyncHandler | AsyncHandler

# Middleware types
MiddlewareFunc = Callable[..., Any]  # Generic middleware callable

# Error handler types
ExceptionHandler = Callable[..., Coroutine[Any, Any, Any]]

# Lifecycle types
LifespanHandler = Callable[..., Awaitable[None]]
