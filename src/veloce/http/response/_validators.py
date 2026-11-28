"""Response validators — ETags, conditional requests, content disposition and cookie removal."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from typing_extensions import Self

from veloce._constants import (
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_DISPOSITION,
    HEADER_CONTENT_LENGTH,
    HEADER_ETAG,
    HEADER_LAST_MODIFIED,
    HEADER_VALUE_ATTACHMENT,
)
from veloce._internal import (
    _etag_matches_strong,
    _preconditions_say_unchanged,
)
from veloce.enums import SameSite
from veloce.http.cache_control import CacheControl
from veloce.http.dates import parse_date

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response._headers import (
    _format_content_disposition,
    header_get,
    header_pop,
)
from veloce.http.response._headers_mixin import HeadersMixin
from veloce.status import (
    HTTP_304_NOT_MODIFIED,
)


class ValidatorsMixin(HeadersMixin):
    """Validators and conditional-request handling."""

    __slots__ = ()

    def set_etag(self, etag: str, *, weak: bool = False) -> None:
        """Set the `ETag` header from an explicit value.

        Quotes the value if the caller passed it bare. Prepends `W/`
        when `weak=True`. Use `add_etag()` for body-derived MD5
        ETags; `set_etag` is for callers that already have an
        authoritative tag (DB revision, commit hash, version
        counter).
        """
        if not etag.startswith('"'):
            etag = f'"{etag}"'
        if weak and not etag.startswith("W/"):
            etag = "W/" + etag
        self.headers[HEADER_ETAG] = etag
        self._encoded = None

    def get_etag(self) -> tuple[str | None, bool]:
        """Return `(etag, is_weak)` parsed from the `ETag` header.

        `(None, False)` when unset. Returned tag keeps its quotes so
        it compares directly with `If-None-Match` values.
        """
        raw = header_get(self.headers, HEADER_ETAG)
        if not raw:
            return (None, False)
        if raw.startswith("W/"):
            return (raw[2:], True)
        return (raw, False)

    def freeze(self) -> None:
        """Pre-compute the cached HTTP/1.1 encode.

        For buffered responses, populates `_encoded` so subsequent
        access pays no encode cost. For streaming responses, no-op.
        Used by response caching layers that want immutable bytes.
        """
        if self._stream is not None:
            return
        if self._encoded is None:
            self.encode()

    @property
    def cache_control(self) -> CacheControl:
        """Parsed `Cache-Control` header (read-only view).

        For setting directives, prefer `set_cache_control(...)` which
        writes the header directly. This property is convenient for
        introspection: `resp.cache_control.max_age`,
        `resp.cache_control.no_store`, etc.
        """
        return CacheControl(header_get(self.headers, HEADER_CACHE_CONTROL) or "")

    def add_etag(self, *, weak: bool = False) -> str:
        """Compute and attach an ETag derived from the body.

        Uses MD5 of the response body, opaque-quoted per RFC 9110 Sec. 8.8.3.
        `weak=True` prepends `W/`. Overwrites any `ETag` already set; pass the
        explicit one in `__init__(headers=...)` to skip this. Returns the value.
        """
        # `usedforsecurity=False`: an opaque cache validator must not raise on FIPS.
        digest = hashlib.md5(self.body, usedforsecurity=False).hexdigest()
        etag = f'"{digest}"' if not weak else f'W/"{digest}"'
        self.headers[HEADER_ETAG] = etag
        self._encoded = None
        return etag

    def make_conditional(self, request: Any) -> Self:
        """Downgrade this response to 304 on a precondition match.

        The request's preconditions are compared against the response's ETag
        and Last-Modified. Checks `If-None-Match` first (per RFC 9110
        Sec. 13.2 precedence),
        then `If-Modified-Since`. On a match, mutates `self` to status
        304 with no body. Returns `self` so callers can use it inline:
        `return resp.make_conditional(request)`.

        Handles `If-None-Match: *` (matches any current representation
        of the resource) and the weak/strong ETag comparison rules.
        """
        # Field names are case-insensitive (RFC 9110 Sec. 5.1), so `Etag` is honored.
        ours_etag = header_get(self.headers, HEADER_ETAG) or ""
        inm = getattr(request, "if_none_match", ())
        if inm:
            # With no ETag it is not a match, and Sec. 13.2 still ranks it first.
            if ours_etag and _preconditions_say_unchanged(inm, None, ours_etag, None):
                self._downgrade_to_304()
            return self

        # If-Modified-Since, only consulted when If-None-Match is absent.
        ours_lm = header_get(self.headers, HEADER_LAST_MODIFIED) or ""
        ims = getattr(request, "if_modified_since", None)
        if ims is not None and ours_lm:
            ours_dt = parse_date(ours_lm)
            if ours_dt is None:
                return self
            if _preconditions_say_unchanged((), ims, ours_etag, ours_dt.timestamp()):
                self._downgrade_to_304()
        return self

    def check_preconditions(self, request: Any) -> Self:
        """Enforce the write-side `If-Match` precondition (RFC 9110 Sec. 13.1.1).

        Raises `PreconditionFailed` (412) when the request carries an
        `If-Match` header that the response's current ETag does not satisfy
        under the strong comparison (Sec. 8.8.3.1) - the lost-update guard.
        `If-Match: *` is satisfied whenever a current representation exists,
        approximated here by the presence of an ETag header. With no
        `If-Match` header the response is returned unchanged. Returns `self`
        so it can be chained: `return resp.check_preconditions(request)`.

        Invoke this inside a handler (where `HTTPException` is converted to a
        response); it raises rather than mutating the status.
        """
        if_match = getattr(request, "if_match", ())
        if not if_match:
            # RFC 9110 Sec. 13.2.2: evaluated only when `If-Match` is absent.
            return self._check_unmodified_since(request)
        # `If-Match: *` is an existence precondition (RFC 9110 Sec. 13.1.1).
        if if_match == ("*",):
            return self
        # `headers` is a plain dict; accept either spelling, as other helpers do.
        ours_etag = header_get(self.headers, HEADER_ETAG) or ""
        for tag in if_match:
            if _etag_matches_strong(ours_etag, tag):
                return self
        from veloce.exceptions import PreconditionFailed  # avoids response <-> exceptions cycle

        raise PreconditionFailed

    def _check_unmodified_since(self, request: Any) -> Self:
        """Enforce `If-Unmodified-Since` (RFC 9110 Sec. 13.1.4), or return self.

        Compared at HTTP-date resolution: the header carries whole seconds, so a
        `Last-Modified` with a fractional part must be floored or every
        comparison of a resource modified within the same second fails.
        """
        since = getattr(request, "if_unmodified_since", None)
        if since is None:
            return self
        raw = header_get(self.headers, HEADER_LAST_MODIFIED)
        if not raw:
            # Nothing to compare, so this is not a reason to refuse.
            return self
        parsed = parse_date(raw)
        # Whole seconds: an HTTP-date carries no finer resolution.
        if parsed is None or int(parsed.timestamp()) <= int(since):
            return self
        from veloce.exceptions import PreconditionFailed  # response <-> exceptions cycle

        raise PreconditionFailed

    def _downgrade_to_304(self) -> None:
        """Strip body + flip status to 304. Used by `make_conditional`."""
        # RFC 9110 Sec. 8.6: a 304 may carry the `Content-Length` a 200 would have,
        # so it is recorded before the body is dropped - a zero recomputed from
        # the emptied body is written over the cache's stored length (RFC 9111
        # Sec. 4.3.4). Idempotent: two callers must not re-record that zero.
        if self.status_code == HTTP_304_NOT_MODIFIED:
            return
        # A streamed length comes from the producer's header; unknown is omitted.
        declared = header_get(self.headers, HEADER_CONTENT_LENGTH)
        representation_length = declared if self._stream is not None else str(len(self.body))
        self.status_code = HTTP_304_NOT_MODIFIED
        self.body = b""
        # A 304 carries no content, so the stream drops with the body.
        self._stream = None
        header_pop(self.headers, HEADER_CONTENT_LENGTH)
        if representation_length is not None:
            self.headers[HEADER_CONTENT_LENGTH] = representation_length
        self._encoded = None

    def set_content_disposition(
        self, disposition: str = HEADER_VALUE_ATTACHMENT, filename: str | None = None
    ) -> str:
        """Write a `Content-Disposition` header - RFC 6266.

        `disposition` is `"attachment"` (force download) or `"inline"` (render
        in-browser). A quotable ASCII `filename` uses `filename="..."`; any
        other name uses only the RFC 5987 `filename*=UTF-8''...` form. Returns
        the header value written.
        """
        value = _format_content_disposition(disposition, filename) if filename else disposition
        self.headers[HEADER_CONTENT_DISPOSITION] = value
        self._encoded = None
        return value

    def delete_cookie(
        self,
        key: str,
        path: str = "/",
        domain: str | None = None,
        *,
        secure: bool = False,
        httponly: bool = False,
        samesite: SameSite | str | None = None,
        partitioned: bool = False,
        prefix: Literal["host", "secure"] | None = None,
    ) -> None:
        """Delete a cookie by overwriting it with an empty value + Max-Age=0.

        The browser only treats the new cookie as a replacement for the
        existing one if `Path`, `Domain`, **and the `Secure` / `SameSite`
        / `Partitioned` attributes match** - otherwise it stores both. So a
        session cookie originally set with `Secure; SameSite=None` (or with
        `Partitioned`) will not be deleted by a plain `delete_cookie(key)`
        call. Pass the same flags here. `prefix` deletes the cookie under
        its true `__Host-`/`__Secure-` wire name and enforces the same
        invariants on the deletion's attributes.
        """
        self.set_cookie(
            key,
            "",
            max_age=0,
            path=path,
            domain=domain,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
            partitioned=partitioned,
            prefix=prefix,
        )
