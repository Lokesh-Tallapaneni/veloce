"""Veloce JSON hello-world — bench target."""

from veloce import Veloce

app = Veloce(openapi_url=None)


@app.get("/")
async def hello() -> dict:
    return {"hello": "world"}
