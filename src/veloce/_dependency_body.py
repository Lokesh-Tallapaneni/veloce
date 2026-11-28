"""Body and upload resolution — the `DependencyResolver` methods that bind a request body."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from veloce._constants import MSG_FIELD_REQUIRED
from veloce._dependency_coerce import _err_body_validation, _msgspec_validation_error
from veloce._handler_plan import MARKER_LOC, MK_COOKIE, MK_FORM, MK_HEADER
from veloce._internal import json_body_refused
from veloce._model_backend import ModelBackend, _msgspec, adapter_for
from veloce.exceptions import HTTPException, RequestValidationError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.datastructures import Cookies, FormData, Headers, QueryParams

from veloce.http.request import Request


class _BodyResolutionMixin:
    """Binds request bodies for `DependencyResolver`; holds no state of its own."""

    __slots__ = ()

    async def _resolve_upload_file(
        self, slot: Any, request: Request, kwargs: dict[str, Any]
    ) -> None:
        """Bind an uploaded file from the multipart form, if present.

        Leaves the kwarg unset when the field is absent and not optional, so the
        handler's own default applies.
        """
        upload = (await request.form()).get(slot.name)
        if upload is not None:
            kwargs[slot.name] = upload
        elif slot.is_optional:
            kwargs[slot.name] = None

    async def _resolve_model_group(self, slot: Any, request: Request) -> Any:
        """Bind a model whose fields come from one request source.

        `Annotated[Filters, Query()]` reads every field of `Filters` off the
        query string rather than one key named `filters`. Errors are remapped
        onto `["query", "<field>"]`, so a bad `limit` blames `limit`.
        """
        mk = slot.marker_kind
        # Four branches, four mappings; only `getlist` and `get` are read.
        source: FormData | Headers | Cookies | QueryParams
        if mk == MK_FORM:
            source = await request.form()
        elif mk == MK_HEADER:
            source = request.headers
        elif mk == MK_COOKIE:
            source = request.cookies
        else:
            source = request.query_params

        getlist = getattr(source, "getlist", None)
        raw: dict[str, Any] = {}
        for validate_key, key, is_list in slot.group_fields:
            if is_list and getlist is not None:
                values = getlist(key.lower() if mk == MK_HEADER else key)
                if values:
                    raw[validate_key] = values
                continue
            value = source.get(key.lower() if mk == MK_HEADER else key)
            if value is not None:
                raw[validate_key] = value

        loc = MARKER_LOC[mk]
        if not raw and (slot.is_optional or slot.marker.has_default):
            # Fall back rather than reporting every field as missing.
            return slot.marker.resolve_default() if slot.marker.has_default else None
        try:
            if slot.backend == ModelBackend.MSGSPEC:
                return _msgspec.convert(raw, type=slot.model, strict=False)
            if slot.backend == ModelBackend.ADAPTED:
                return adapter_for(slot.model).validate_python(raw)
            return slot.model.model_validate(raw)
        except PydanticValidationError as e:
            raise _err_body_validation(e, loc) from e
        except Exception as err:
            raise RequestValidationError(
                [{"loc": [loc], "msg": str(err), "type": "value_error"}]
            ) from err

    async def _resolve_body_model(self, slot: Any, request: Request) -> Any:
        if slot.backend == ModelBackend.MSGSPEC:
            return await self._resolve_msgspec_body(slot, request)
        if slot.backend == ModelBackend.ADAPTED:
            return await self._resolve_adapted_body(slot, request)
        return await self._resolve_pydantic_body(slot, request)

    @staticmethod
    def _require_json_body(request: Request) -> None:
        """Refuse a body whose `Content-Type` declares it is not JSON.

        Which content types are refused lives in `json_body_refused`, so this
        door and `await request.json()` cannot answer differently. What this adds
        is the carve-out for an absent *body*: nothing was there to be sent
        cross-origin, so an omitted optional body is answered "absent" below.
        """
        mimetype = request.mimetype
        if not json_body_refused(mimetype):
            return
        if request._body_drained and not request._body.strip():
            return
        raise RequestValidationError(
            [
                {
                    "loc": ["body"],
                    "msg": f"Expected a JSON body; got Content-Type {mimetype!r}",
                    "type": "value_error",
                }
            ]
        )

    async def _resolve_adapted_body(self, slot: Any, request: Request) -> Any:
        """Validate a dataclass / `TypedDict` body through its cached adapter."""
        self._require_json_body(request)
        raw = await request.body()
        if not raw.strip():
            if slot.is_optional and slot.has_default:
                return slot.default
            raise RequestValidationError(
                [{"loc": ["body"], "msg": MSG_FIELD_REQUIRED, "type": "missing"}]
            )
        try:
            return adapter_for(slot.model).validate_json(raw)
        except PydanticValidationError as e:
            raise _err_body_validation(e) from e

    async def _resolve_pydantic_body(self, slot: Any, request: Request) -> Any:
        self._require_json_body(request)
        try:
            body_data = await request.json()
            # `request.json()`, not `.body()` - an MCP call never buffers bytes.
            # Only a default makes the body omittable, not `is_optional` alone.
            if body_data is None and slot.is_optional and slot.has_default:
                return slot.default
            return slot.model.model_validate(body_data)
        except PydanticValidationError as e:
            raise _err_body_validation(e) from e
        except ValidationError:
            raise
        except HTTPException:
            # `on_json_loading_failed` already raised `BadRequest` with a stable
            # message, the decoder's offsets deriving from attacker-controlled input.
            raise
        except Exception as err:
            raise RequestValidationError(
                [
                    {
                        "loc": ["body"],
                        "msg": "Invalid request body",
                        "type": "value_error",
                    }
                ]
            ) from err

    async def _resolve_msgspec_body(self, slot: Any, request: Request) -> Any:
        # Only for a msgspec.Struct slot, tagged only when msgspec is installed.
        self._require_json_body(request)
        raw = await request.body()
        # "missing", not the opaque truncation error `decode(b"")` would raise.
        if not raw or not raw.strip():
            if slot.is_optional and slot.has_default:
                return slot.default
            raise RequestValidationError(
                [{"loc": ["body"], "msg": "Missing request body", "type": "missing"}]
            )
        try:
            # `strict=True` rejects a `str` for an `int` rather than coercing.
            return _msgspec.json.decode(raw, type=slot.model)
        except _msgspec.ValidationError as e:
            # `ValidationError` SUBCLASSES `DecodeError`, so it is caught first; its
            # field path is in the message text, so `loc` stays `["body"]`.
            raise RequestValidationError(
                [{"loc": ["body"], "msg": str(e), "type": "value_error"}]
            ) from e
        except _msgspec.DecodeError as e:
            raise RequestValidationError(
                [{"loc": ["body"], "msg": "Invalid JSON body", "type": "value_error"}]
            ) from e

    @staticmethod
    def _validate_marker_body(value: Any, slot: Any) -> Any:
        """Validate an already-extracted `Body()` value against the slot's model.

        `payload: Payload = Body()` declares the same contract as a bare
        `payload: Payload`, down to the `["body", ...]` error location. It cannot
        reuse `_resolve_body_model`, which reads the whole body, because
        `embed=True` takes the value from under the parameter name.
        """
        backend = slot.backend
        try:
            if backend == ModelBackend.MSGSPEC:
                return _msgspec.convert(value, type=slot.model, strict=False)
            if backend == ModelBackend.ADAPTED:
                return adapter_for(slot.model).validate_python(value)
            return slot.model.model_validate(value)
        except PydanticValidationError as e:
            raise _err_body_validation(e) from e
        except _msgspec_validation_error as e:
            # The field path is in the message text, so `loc` stays `["body"]`.
            raise RequestValidationError(
                [{"loc": ["body"], "msg": str(e), "type": "value_error"}]
            ) from e
