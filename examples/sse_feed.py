"""A Server-Sent Events live feed.

Shows one-way streaming with ``EventSourceResponse`` and ``ServerSentEvent``:
the handler returns an async generator that yields a timestamped event once a
second, with named events and a keep-alive ``ping``. A served HTML page uses
the browser ``EventSource`` API to display the stream.

Run it::

    python examples/sse_feed.py

Then open http://localhost:8000/ in a browser, or stream from the shell::

    curl -N localhost:8000/events
"""

from __future__ import annotations

import asyncio
import time

from veloce import EventSourceResponse, HTMLResponse, Request, ServerSentEvent, Veloce

app = Veloce(title="SSE Feed")

_PAGE = """\
<!doctype html>
<title>Veloce SSE feed</title>
<h1>Live feed</h1>
<ul id="log"></ul>
<script>
  const source = new EventSource("/events");
  source.addEventListener("tick", (e) => {
    const li = document.createElement("li");
    li.textContent = JSON.parse(e.data).time;
    document.getElementById("log").appendChild(li);
  });
</script>
"""


@app.get("/")
async def index():
    return HTMLResponse(_PAGE)


@app.get("/events")
async def events(request: Request):
    async def generate():
        counter = 0
        while True:
            counter += 1
            yield ServerSentEvent.json(
                {"time": time.strftime("%H:%M:%S"), "n": counter},
                event="tick",
                id=str(counter),
            )
            await asyncio.sleep(1)

    # ``ping`` emits a keep-alive comment every 15s of silence so proxies
    # do not drop the idle connection.
    return EventSourceResponse(generate(), ping=15)


if __name__ == "__main__":
    app.run(port=8000)
