"""MCP registry base — the shared name/URI -> primitive table behind every registry.

A concrete registry supplies only what differs: the key it indexes by and the
message a duplicate raises, exposing its own backing dict through `_store`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

from veloce._internal import _require_methods

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.descriptors import MCPDescriptor

T = TypeVar("T", bound="MCPDescriptor")


class Registry(Generic[T]):
    """Base name/URI -> primitive table: deduplicating `register()` plus `get()`."""

    # Empty slots keep the base from adding a `__dict__` to its
    # `@dataclass(slots=True)` subclasses.
    __slots__ = ()

    #: What a concrete registry must supply, checked at definition: the first
    #: registration is import-time for a decorated tool, already too late.
    _required = ("_store", "_key", "_duplicate_message")

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _require_methods(cls, Registry, Registry._required)

    @property
    def _store(self) -> dict[str, T]:
        """Return the backing identifier -> primitive dict the concrete registry owns."""
        raise NotImplementedError

    def _key(self, item: T) -> str:
        """Return the identifier `item` is stored under (its name, or a URI)."""
        raise NotImplementedError

    def _duplicate_message(self, key: str) -> str:
        """Return the error text for a duplicate `key`, phrased per primitive."""
        raise NotImplementedError

    def register(self, item: T) -> None:
        """Add `item`, rejecting a key already present with a primitive-specific error."""
        key = self._key(item)
        store = self._store
        if key in store:
            raise ValueError(self._duplicate_message(key))
        store[key] = item

    def get(self, name: str) -> T | None:
        """Return the primitive registered under `name`, or `None`."""
        return self._store.get(name)
