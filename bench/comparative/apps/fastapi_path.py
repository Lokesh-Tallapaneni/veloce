"""FastAPI path-param routing — bench target."""

from fastapi import FastAPI

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/items/{item_id}")
async def item(item_id: int) -> dict:
    return {"id": item_id, "name": f"item-{item_id}"}
