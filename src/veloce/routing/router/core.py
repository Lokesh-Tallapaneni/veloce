"""Router — radix-tree routing with path parameters, method dispatch, and route groups."""

from __future__ import annotations

from veloce.routing.router.merging import MergingMixin


class Router(MergingMixin):
    """High-performance radix-tree router with a decorator-based route API.

    Usage::

        from veloce import Router, Veloce

        api = Router(prefix="/api")

        @api.get("/items/{item_id:int}")
        async def get_item(item_id: int):
            return {"item_id": item_id}

        app = Veloce()
        app.include_router(api)
    """
