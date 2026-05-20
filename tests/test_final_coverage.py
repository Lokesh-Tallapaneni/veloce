"""Final coverage tests — features that existed but lacked test coverage."""

import asyncio

import orjson
import pytest

from tests.conftest import make_request
from veloce import JSONResponse, Request, Veloce
from veloce.http.datastructures import UploadFile


class TestMiddlewareHTTPDecorator:
    """Test @app.middleware('http') with the call_next pattern."""

    @pytest.mark.asyncio
    async def test_middleware_http_modifies_response(self):
        app = Veloce(openapi_url=None)

        @app.middleware("http")
        async def add_timing(request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Process"] = "true"
            response._encoded = None
            return response

        @app.get("/data")
        async def data(request: Request):
            return {"value": 42}

        resp = await app.handle_request(make_request(path="/data"))
        assert resp.status_code == 200
        assert resp.headers.get("X-Process") == "true"
        assert orjson.loads(resp.body)["value"] == 42

    @pytest.mark.asyncio
    async def test_middleware_http_short_circuit(self):
        app = Veloce(openapi_url=None)

        @app.middleware("http")
        async def block_everything(request: Request, call_next):
            if request.path == "/blocked":
                return JSONResponse({"error": "blocked"}, status_code=403)
            return await call_next(request)

        @app.get("/blocked")
        async def blocked(request: Request):
            return {"should_not": "reach"}

        @app.get("/allowed")
        async def allowed(request: Request):
            return {"ok": True}

        resp = await app.handle_request(make_request(path="/blocked"))
        assert resp.status_code == 403

        resp = await app.handle_request(make_request(path="/allowed"))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_multiple_http_middleware_chain(self):
        app = Veloce(openapi_url=None)
        order = []

        @app.middleware("http")
        async def mw1(request: Request, call_next):
            order.append("mw1_before")
            response = await call_next(request)
            order.append("mw1_after")
            return response

        @app.middleware("http")
        async def mw2(request: Request, call_next):
            order.append("mw2_before")
            response = await call_next(request)
            order.append("mw2_after")
            return response

        @app.get("/chain")
        async def chain(request: Request):
            order.append("handler")
            return {"ok": True}

        await app.handle_request(make_request(path="/chain"))
        assert order == ["mw1_before", "mw2_before", "handler", "mw2_after", "mw1_after"]


class TestOnEventDecorators:
    """Test startup/shutdown event decorators."""

    @pytest.mark.asyncio
    async def test_on_event_startup(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.on_event("startup")
        async def startup():
            log.append("started")

        await app._run_lifecycle("startup")
        assert "started" in log

    @pytest.mark.asyncio
    async def test_on_event_shutdown(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.on_event("shutdown")
        async def shutdown():
            log.append("stopped")

        await app._run_lifecycle("shutdown")
        assert "stopped" in log

    @pytest.mark.asyncio
    async def test_on_startup_decorator(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.on_startup
        async def init_db():
            log.append("db_ready")

        await app._run_lifecycle("startup")
        assert "db_ready" in log


class TestUploadFileContextManager:
    """Test UploadFile async context manager."""

    @pytest.mark.asyncio
    async def test_async_with(self):
        import io

        async with UploadFile(filename="test.txt", file=io.BytesIO(b"hello")) as f:
            data = await f.read()
            assert data == b"hello"
        # File should be closed after exiting context
        assert f.file.closed


class TestTestClientNoWarning:
    """Verify TestClient doesn't trigger pytest collection warning."""

    def test_testclient_has_test_false(self):
        from veloce.testclient import TestClient

        assert TestClient.__test__ is False


class TestConfigAndExtensions:
    """Test config, secret_key, extensions."""

    @pytest.mark.asyncio
    async def test_config_accessible_from_request(self):
        app = Veloce(openapi_url=None)
        app.config["API_KEY"] = "secret123"

        @app.get("/config")
        async def get_config(request: Request):
            return {"key": request.app.config["API_KEY"]}

        resp = await app.handle_request(make_request(path="/config"))
        assert orjson.loads(resp.body)["key"] == "secret123"

    @pytest.mark.asyncio
    async def test_secret_key_from_request(self):
        app = Veloce(openapi_url=None)
        app.secret_key = "super-secret"

        @app.get("/secret")
        async def get_secret(request: Request):
            return {"has_secret": request.app.secret_key is not None}

        resp = await app.handle_request(make_request(path="/secret"))
        assert orjson.loads(resp.body)["has_secret"] is True


class TestSendFromDirectory:
    """Test send_from_directory helper."""

    def test_send_existing_file(self, tmp_path):
        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello World")

        from veloce.helpers import send_from_directory

        resp = send_from_directory(str(tmp_path), "hello.txt")
        assert resp.body == b"Hello World"

    def test_directory_traversal_blocked(self, tmp_path):
        from veloce.exceptions import HTTPException
        from veloce.helpers import send_from_directory

        with pytest.raises((HTTPException, FileNotFoundError)):
            send_from_directory(str(tmp_path), "../../../etc/passwd")


class TestFlashMessages:
    """Test flash/get_flashed_messages."""

    def test_flash_and_retrieve(self):
        from veloce.helpers import flash, g, get_flashed_messages

        g._reset()

        flash("Item created", "success")
        flash("Check email", "info")

        messages = get_flashed_messages()
        assert messages == ["Item created", "Check email"]

        # Messages should be consumed
        assert get_flashed_messages() == []

    def test_flash_with_categories(self):
        from veloce.helpers import flash, g, get_flashed_messages

        g._reset()

        flash("Error occurred", "error")
        flash("All good", "success")

        messages = get_flashed_messages(with_categories=True)
        assert messages == [("error", "Error occurred"), ("success", "All good")]

    def test_flash_category_filter(self):
        from veloce.helpers import flash, g, get_flashed_messages

        g._reset()

        flash("Error 1", "error")
        flash("Success 1", "success")
        flash("Error 2", "error")

        errors = get_flashed_messages(category_filter=["error"])
        assert len(errors) == 2


class TestWebSocketTimeout:
    """Test WebSocket receive with timeout."""

    @pytest.mark.asyncio
    async def test_receive_timeout(self):
        from veloce.websocket import WebSocket

        class FakeTransport:
            def write(self, data):
                pass

            def close(self):
                pass

            def get_extra_info(self, key):
                return None

        ws = WebSocket(FakeTransport(), {"sec-websocket-key": "test"})

        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_text(timeout=0.01)
