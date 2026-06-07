"""A broadcast WebSocket chat room.

Shows the imperative WebSocket API: accept the handshake, loop over inbound
text frames, and fan each message out to every other connected client. A
served HTML page provides a minimal browser client so the example is testable
end to end.

Run it::

    python examples/websocket_chat.py

Then open http://localhost:8000/ in two browser tabs and type in each.
"""

from __future__ import annotations

from veloce import HTMLResponse, Veloce, WebSocket, WebSocketDisconnect

app = Veloce(title="WebSocket Chat")

# The set of live connections. Each broadcast iterates a snapshot so a peer
# disconnecting mid-send does not mutate the set under us.
_clients: set[WebSocket] = set()

_PAGE = """\
<!doctype html>
<title>Veloce chat</title>
<h1>Chat</h1>
<input id="msg" autocomplete="off" placeholder="Say something" />
<button onclick="send()">Send</button>
<ul id="log"></ul>
<script>
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => {
    const li = document.createElement("li");
    li.textContent = e.data;
    document.getElementById("log").appendChild(li);
  };
  function send() {
    const input = document.getElementById("msg");
    ws.send(input.value);
    input.value = "";
  }
</script>
"""


@app.get("/")
async def index():
    return HTMLResponse(_PAGE)


async def broadcast(message: str, sender: WebSocket) -> None:
    for client in list(_clients):
        if client is sender:
            continue
        try:
            await client.send_text(message)
        except Exception:
            _clients.discard(client)


@app.websocket("/ws")
async def chat(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    await broadcast("a user joined", ws)
    try:
        async for message in ws.iter_text():
            await broadcast(message, ws)
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        await broadcast("a user left", ws)


if __name__ == "__main__":
    app.run(port=8000)
