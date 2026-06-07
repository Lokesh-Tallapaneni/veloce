"""In-memory CRUD API.

Shows the core REST pattern in Veloce: Pydantic request bodies, typed path
parameters, dependency injection for shared state, and HTTPException for
not-found errors. The "database" is a plain dict, so the file runs with no
external services.

Run it::

    python examples/crud_api.py

Then try::

    curl -X POST localhost:8000/items -d '{"name":"Widget","price":9.99}' \
        -H "Content-Type: application/json"
    curl localhost:8000/items
    curl localhost:8000/items/1
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Depends, HTTPException, Veloce

app = Veloce(title="CRUD API", version="1.0.0")

# A trivial in-memory store. A real app would inject a database handle here.
_items: dict[int, dict] = {}
_next_id = 0


def get_store() -> dict[int, dict]:
    return _items


class ItemCreate(BaseModel):
    name: str
    price: float
    in_stock: bool = True


class ItemUpdate(BaseModel):
    name: str | None = None
    price: float | None = None
    in_stock: bool | None = None


@app.get("/items")
async def list_items(store: dict = Depends(get_store)):
    return [{"id": item_id, **data} for item_id, data in store.items()]


@app.post("/items")
async def create_item(item: ItemCreate, store: dict = Depends(get_store)):
    global _next_id
    _next_id += 1
    store[_next_id] = item.model_dump()
    return {"id": _next_id, **store[_next_id]}, 201


@app.get("/items/{item_id}")
async def get_item(item_id: int, store: dict = Depends(get_store)):
    if item_id not in store:
        raise HTTPException(404, f"Item {item_id} not found")
    return {"id": item_id, **store[item_id]}


@app.patch("/items/{item_id}")
async def update_item(item_id: int, patch: ItemUpdate, store: dict = Depends(get_store)):
    if item_id not in store:
        raise HTTPException(404, f"Item {item_id} not found")
    changes = patch.model_dump(exclude_none=True)
    store[item_id].update(changes)
    return {"id": item_id, **store[item_id]}


@app.delete("/items/{item_id}")
async def delete_item(item_id: int, store: dict = Depends(get_store)):
    if store.pop(item_id, None) is None:
        raise HTTPException(404, f"Item {item_id} not found")
    return {"deleted": item_id}


if __name__ == "__main__":
    app.run(port=8000)
