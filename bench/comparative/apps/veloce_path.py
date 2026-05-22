"""Veloce path-param routing — bench target."""

from veloce import Veloce

app = Veloce(openapi_url=None)


@app.get("/items/{item_id}")
async def item(item_id: int) -> dict:
    return {"id": item_id, "name": f"item-{item_id}"}
