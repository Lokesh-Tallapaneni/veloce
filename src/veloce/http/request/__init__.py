"""Request package — gateway preserving the `veloce.http.request` import paths.

`Request` is `request.Request` over four layers, each a mixin on the one before
it: `_base` (slotted state, headers and body metadata), `_negotiation` (Accept,
validators, cookies), `_url` (URL, matched route, connection) and `_body` (body,
form, JSON and per-request state). `State` and `Address` are re-exported because
callers have always imported them from here.

`_split_etag_list` is re-exported deliberately: tests reach it through the
package path.
"""

from __future__ import annotations

from veloce.http.datastructures import Address, State
from veloce.http.request._negotiation import _split_etag_list as _split_etag_list
from veloce.http.request.request import Request

__all__ = ["Address", "Request", "State"]
