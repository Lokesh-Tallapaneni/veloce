"""Flask path-param routing wrapped as ASGI — bench target."""

from asgiref.wsgi import WsgiToAsgi
from flask import Flask

wsgi_app = Flask(__name__)


@wsgi_app.get("/items/<int:item_id>")
def item(item_id: int) -> dict:
    return {"id": item_id, "name": f"item-{item_id}"}


app = WsgiToAsgi(wsgi_app)
