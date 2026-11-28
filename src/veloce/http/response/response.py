"""Response — the public base response, assembled from its focused layers."""

from __future__ import annotations

from veloce.http.response._validators import ValidatorsMixin


class Response(ValidatorsMixin):
    """Base HTTP response.

    Usage::

        from veloce import Response

        async def handler(request):
            return Response(body=b"hello", content_type="text/plain")
    """

    __slots__ = ()
