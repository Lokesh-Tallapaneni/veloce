"""Response package — gateway preserving the `veloce.http.response` import paths.

`Response` is `response.Response` over three layers, each a mixin on the one
before it: `_base` (slotted state, body, status and wire encoding),
`_headers_mixin` (typed header accessors, cookies, caching) and `_validators`
(ETags and conditional requests). The concrete types live beside it: `json`,
`text`, `streaming` and `file`. Every name is re-exported here, so each
`from veloce.http.response import X` path keeps resolving unchanged.

The private names below are re-exported deliberately: tests and sibling modules
reach them through the package path.
"""

from __future__ import annotations

from veloce.http.response._headers import (
    _MAX_VARY_MERGES as _MAX_VARY_MERGES,
)
from veloce.http.response._headers import (
    _VARY_MERGES as _VARY_MERGES,
)
from veloce.http.response._headers import (
    advertised_length,
    header_get,
    header_key,
    header_pop,
    header_present,
)
from veloce.http.response.file import (
    _INLINE_READ_MAX as _INLINE_READ_MAX,
)
from veloce.http.response.file import (
    FILE_STREAM_CHUNK,
    FileResponse,
)
from veloce.http.response.file import (
    _stream_file as _stream_file,
)
from veloce.http.response.json_response import JSONResponse, ORJSONResponse, UJSONResponse
from veloce.http.response.response import Response
from veloce.http.response.streaming import StreamingResponse
from veloce.http.response.text_response import HTMLResponse, PlainTextResponse, RedirectResponse

__all__ = [
    "FILE_STREAM_CHUNK",
    "FileResponse",
    "HTMLResponse",
    "JSONResponse",
    "ORJSONResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "Response",
    "StreamingResponse",
    "UJSONResponse",
    "advertised_length",
    "header_get",
    "header_key",
    "header_pop",
    "header_present",
]
