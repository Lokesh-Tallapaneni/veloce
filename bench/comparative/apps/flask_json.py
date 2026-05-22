"""Flask JSON hello-world wrapped as ASGI — bench target.

Flask is WSGI-native; we run it under uvicorn through `asgiref.WsgiToAsgi`
so all three frameworks share the same server runtime and the only
variable is the framework itself.
"""

from asgiref.wsgi import WsgiToAsgi
from flask import Flask

wsgi_app = Flask(__name__)


@wsgi_app.get("/")
def hello() -> dict:
    return {"hello": "world"}


app = WsgiToAsgi(wsgi_app)
