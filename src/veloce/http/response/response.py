"""Response — the public base response, assembled from its focused layers."""

from __future__ import annotations

from veloce.http.response._base import ResponseBase
from veloce.http.response._validators import ValidatorsMixin


class Response(ValidatorsMixin):
    """Base HTTP response.

    Usage::

        from veloce import Response

        async def handler(request):
            return Response(body=b"hello", content_type="text/plain")
    """

    __slots__ = ()


# The constructor lives three classes down, on `ResponseBase`, and every
# subclass reaches it through `super().__init__`, which scans the MRO for it on
# each construction. Naming it on this class puts it on the first class the scan
# visits, as when this class defined it: measured at about 40 ns per response,
# and an alias costs nothing per call. Assigned after the class body because
# inside it the type checker reads the alias as a method override.
Response.__init__ = ResponseBase.__init__  # type: ignore[method-assign]  # a constructor alias, see above
