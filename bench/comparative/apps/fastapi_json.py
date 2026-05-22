"""FastAPI JSON hello-world — bench target."""

from fastapi import FastAPI

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def hello() -> dict:
    return {"hello": "world"}
