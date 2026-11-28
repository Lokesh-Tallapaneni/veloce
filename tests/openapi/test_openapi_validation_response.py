"""Auto-generated 422 validation-error response in the OpenAPI document.

The dependency resolver raises ``RequestValidationError`` (rendered as a 422
``{"detail": [{"loc", "msg", "type"}, ...]}``) whenever a request-bound
parameter fails validation. The generated document advertises that response for
every operation with a validatable parameter, references the shared
``HTTPValidationError`` component, omits it for parameterless operations, and
yields to a user-declared 422.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Depends, Query, Veloce
from veloce.contrib.openapi import get_openapi_schema

_HTTP_VALIDATION_REF = "#/components/schemas/HTTPValidationError"


class _Item(BaseModel):
    name: str


def _responses(app: Veloce, path: str, method: str = "get") -> dict:
    return get_openapi_schema(app)["paths"][path][method]["responses"]


def test_query_param_route_gets_422_and_component() -> None:
    app = Veloce()

    @app.get("/items")
    async def items(request, n: int = Query(default=1)):
        return {}

    schema = get_openapi_schema(app)
    resp = schema["paths"]["/items"]["get"]["responses"]
    assert "422" in resp
    assert resp["422"]["content"]["application/json"]["schema"]["$ref"] == _HTTP_VALIDATION_REF

    components = schema["components"]["schemas"]
    assert "HTTPValidationError" in components
    assert "ValidationError" in components
    # The envelope nests an array of per-error items under `detail`.
    detail = components["HTTPValidationError"]["properties"]["detail"]
    assert detail["type"] == "array"
    assert detail["items"]["$ref"] == "#/components/schemas/ValidationError"
    # Each item carries the loc/msg/type shape the resolver emits.
    item_props = components["ValidationError"]["properties"]
    assert set(item_props) == {"loc", "msg", "type"}


def test_path_param_route_gets_422() -> None:
    app = Veloce()

    @app.get("/items/{item_id}")
    async def item(request, item_id: int):
        return {}

    assert "422" in _responses(app, "/items/{item_id}")


def test_body_route_gets_422() -> None:
    app = Veloce()

    @app.post("/items")
    async def create(request, body: _Item):
        return {}

    assert "422" in _responses(app, "/items", "post")


def test_no_param_route_has_no_422_and_no_component() -> None:
    app = Veloce()

    @app.get("/ping")
    async def ping(request):
        return {}

    schema = get_openapi_schema(app)
    assert "422" not in schema["paths"]["/ping"]["get"]["responses"]
    # No operation referenced the envelope, so the components must not appear.
    components = schema.get("components", {}).get("schemas", {})
    assert "HTTPValidationError" not in components
    assert "ValidationError" not in components


def test_user_declared_422_is_not_overwritten() -> None:
    app = Veloce()

    @app.get("/items", responses={422: {"description": "Custom validation message"}})
    async def items(request, n: int = Query(default=1)):
        return {}

    schema = get_openapi_schema(app)
    resp = schema["paths"]["/items"]["get"]["responses"]
    assert resp["422"]["description"] == "Custom validation message"
    # The auto component is not registered for a user-owned 422 entry.
    assert "HTTPValidationError" not in schema.get("components", {}).get("schemas", {})


def test_user_model_named_httpvalidationerror_does_not_corrupt_422() -> None:
    """A user model occupying the reserved name gets the auto envelope a
    collision-free name, and the 422 `$ref` points at the auto envelope - not
    the unrelated user model.
    """

    class HTTPValidationError(BaseModel):  # user model stealing the reserved name
        code: int

    app = Veloce()

    @app.get("/things", response_model=HTTPValidationError)
    async def things(request, n: int = Query(default=1)):
        return {"code": 1}

    schema = get_openapi_schema(app)
    components = schema["components"]["schemas"]
    # The user model keeps the canonical name.
    assert components["HTTPValidationError"]["properties"] == {
        "code": {"type": "integer", "title": "Code"}
    }
    # The auto envelope was registered under a collision-free name...
    assert "HTTPValidationError_2" in components
    assert "detail" in components["HTTPValidationError_2"]["properties"]
    # ...and the 422 ref points at the auto envelope, never the user model.
    ref = schema["paths"]["/things"]["get"]["responses"]["422"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert ref == "#/components/schemas/HTTPValidationError_2"
    # The auto envelope's nested item ref also resolves to a real component.
    item_ref = components["HTTPValidationError_2"]["properties"]["detail"]["items"]["$ref"]
    assert item_ref.removeprefix("#/components/schemas/") in components


def test_bytes_model_field_documented_as_base64_byte_format() -> None:
    """`bytes` JSON-serialize as base64, so the generated schema documents a
    `bytes` model field as `format: byte` (base64), not `binary` (raw)."""

    class _Blob(BaseModel):
        payload: bytes

    app = Veloce()

    @app.get("/blob", response_model=_Blob)
    async def blob():
        return {"payload": b"hi"}

    components = get_openapi_schema(app)["components"]["schemas"]
    field = components["_Blob"]["properties"]["payload"]
    assert field == {"type": "string", "format": "byte", "title": "Payload"}


def test_sub_dependency_validation_gets_422() -> None:
    """A handler with no top-level params but a `Depends(...)` whose sub-dependency
    validates input still advertises the 422 the resolver can raise."""

    def dep(n: int = Query(default=1)):
        return n

    app = Veloce()

    @app.get("/sub")
    async def sub(value=Depends(dep)):
        return {}

    resp = _responses(app, "/sub")
    assert "422" in resp
    assert resp["422"]["content"]["application/json"]["schema"]["$ref"] == _HTTP_VALIDATION_REF


def test_explicit_422_referencing_user_model_preserved_on_collision() -> None:
    """A user-declared 422 that references a model named `HTTPValidationError`
    keeps its own ref even when another route's auto-422 forces the envelope to a
    collision-free name (the auto entry uses an internal placeholder)."""

    class HTTPValidationError(BaseModel):
        code: int

    app = Veloce()

    @app.get("/explicit", responses={422: {"model": HTTPValidationError, "description": "mine"}})
    async def explicit():
        return {}

    @app.get("/auto")
    async def auto(request, n: int = Query(default=1)):
        return {}

    schema = get_openapi_schema(app)
    explicit_ref = schema["paths"]["/explicit"]["get"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    auto_ref = schema["paths"]["/auto"]["get"]["responses"]["422"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    # Explicit 422 stays on the user model; auto 422 lands on the renamed envelope.
    assert explicit_ref == "#/components/schemas/HTTPValidationError"
    assert auto_ref == "#/components/schemas/HTTPValidationError_2"
    assert schema["components"]["schemas"]["HTTPValidationError"]["properties"] == {
        "code": {"type": "integer", "title": "Code"}
    }


def test_openapi_extra_422_is_preserved_not_overwritten() -> None:
    """A 422 declared via `openapi_extra` (custom shape/media type) is preserved;
    the auto JSON validation-error response is not merged on top of it."""
    app = Veloce()

    @app.get(
        "/x",
        openapi_extra={
            "responses": {
                "422": {
                    "description": "custom",
                    "content": {"text/plain": {"schema": {"type": "string"}}},
                }
            }
        },
    )
    async def x(request, n: int = Query(default=1)):
        return {}

    resp = _responses(app, "/x")
    assert resp["422"]["description"] == "custom"
    assert "text/plain" in resp["422"]["content"]
    assert "application/json" not in resp["422"]["content"]
