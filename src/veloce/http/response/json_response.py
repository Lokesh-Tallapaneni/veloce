"""JSON responses — encoded by the configured JSON backend."""

from __future__ import annotations

from typing import Any

from veloce._internal import (
    MIME_JSON,
    dumps_current,
)

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response.response import Response
from veloce.status import (
    HTTP_200_OK,
)


class JSONResponse(Response):
    """JSON response encoded by the configured JSON backend.

    Usage::

        from veloce import JSONResponse

        async def handler(request):
            return JSONResponse({"ok": True}, status_code=200)
    """

    __slots__ = ()

    # Class default Content-Type, named apart from `Response.media_type`.
    default_media_type: str = MIME_JSON

    def __init__(
        self,
        data: Any,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        background: Any = None,
    ) -> None:
        try:
            # Through the app's provider, so `JSON_SORT_KEYS` reaches this class.
            body = dumps_current(data)
        except TypeError as exc:
            raise ValueError(f"JSONResponse data is not JSON-serializable: {exc}") from exc
        super().__init__(
            status_code=status_code,
            body=body,
            content_type=type(self).default_media_type,
            headers=headers,
            background=background,
        )

    @classmethod
    def _from_encoded(cls, body: bytes) -> JSONResponse:
        """Wrap already-encoded JSON bytes, for a caller that resolved the dialect.

        `from_bytes` is the public form and validates its argument; the dispatch
        path produced these bytes itself, so it needs neither the check nor a
        second encoder lookup.
        """
        resp = cls.__new__(cls)
        Response.__init__(
            resp,
            status_code=HTTP_200_OK,
            body=body,
            content_type=cls.default_media_type,
        )
        return resp

    @classmethod
    def from_bytes(
        cls,
        body: bytes,
        *,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        """Build a `JSONResponse` from already-encoded JSON bytes.

        Skips `__init__`'s re-encode - use this when the caller has
        produced the JSON body itself. The body is sent verbatim with
        `Content-Type` from `cls.default_media_type`, unless `headers` carries a
        `Content-Type`, in which case the caller-supplied value wins.

        The caller is responsible for `body` being valid UTF-8 JSON; nothing is
        parsed or validated. `body` must be `bytes` or `bytearray` - a `str`
        raises `TypeError` rather than being encoded under an assumed charset.

        Args:
            body: The encoded JSON document.
            status_code: HTTP status code.
            headers: Extra response headers; a `Content-Type` here wins.

        Returns:
            The response.

        Raises:
            TypeError: If `body` is not `bytes` or `bytearray`.

        """
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError(
                "JSONResponse.from_bytes() requires bytes or bytearray, "
                f"got {type(body).__name__}. Encode the value first "
                "(e.g. body.encode('utf-8')) or use JSONResponse(data)."
            )
        resp = cls.__new__(cls)
        Response.__init__(
            resp,
            status_code=status_code,
            body=body if isinstance(body, bytes) else bytes(body),
            content_type=cls.default_media_type,
            headers=headers,
        )
        return resp


class ORJSONResponse(JSONResponse):
    """Semantic alias for `JSONResponse`.

    Both encode through the configured JSON backend, which is selected at
    import rather than by the response class.
    """

    __slots__ = ()


class UJSONResponse(Response):
    """JSON response encoded with `ujson`.

    Imports `ujson` at construction, so apps that do not use this class do not
    need the package installed.
    """

    __slots__ = ()

    def __init__(
        self,
        data: Any,
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            import ujson
        except ImportError as err:
            raise ImportError(
                "UJSONResponse requires the `ujson` package. Install it with: pip install ujson"
            ) from err
        body = ujson.dumps(data).encode("utf-8")
        super().__init__(
            status_code=status_code,
            body=body,
            content_type=MIME_JSON,
            headers=headers,
        )
