"""File helpers — `send_file` and `send_from_directory`, and their async forms.

Serve a path as a `FileResponse`, resolving the `Content-Disposition` name, the
cache headers and the `Range` negotiation. They read only the context variables
in `veloce._internal`, so they need nothing from the proxy classes in
`helpers.py`, which re-exports them.
"""

from __future__ import annotations

import os
from typing import Any

from veloce._constants import (
    HEADER_CACHE_CONTROL,
    HEADER_ETAG,
    HEADER_LAST_MODIFIED,
    MSG_ACCESS_DENIED,
)
from veloce._internal import _current_app_var, _current_request_var, _serving_error_page_var
from veloce.http.dates import http_date
from veloce.http.response import FileResponse, Response
from veloce.safe import safe_join
from veloce.status import HTTP_403_FORBIDDEN

# ── send_file() ───────────────────────────────────────────


def _attachment_name(resolved: str, as_attachment: bool, download_name: str | None) -> str | None:
    """Resolve the `Content-Disposition` filename for a file response."""
    if not as_attachment:
        return None
    return download_name or os.path.basename(resolved)


def _build_send_file_args(
    path_or_file: Any,
    as_attachment: bool,
    download_name: str | None,
    last_modified: Any,
    etag: bool | str,
    max_age: int | None,
) -> tuple[str, str | None, dict[str, str], bool]:
    """Compute the path, attachment name, headers, and strip-ETag flag."""
    headers: dict[str, str] = {}
    if last_modified is not None:
        if isinstance(last_modified, str):
            headers[HEADER_LAST_MODIFIED] = last_modified
        else:
            headers[HEADER_LAST_MODIFIED] = http_date(last_modified)

    if isinstance(etag, str):
        headers[HEADER_ETAG] = etag

    if max_age is None:
        # Through the context var, so the helper still works (with no
        # Cache-Control) outside a request.
        app = _current_app_var.get()
        if app is not None:
            max_age = app.config.get("SEND_FILE_MAX_AGE_DEFAULT")
    if max_age is not None:
        headers[HEADER_CACHE_CONTROL] = f"public, max-age={max_age}"

    path = str(path_or_file)
    attachment_name = _attachment_name(path, as_attachment, download_name)
    return path, attachment_name, headers, etag is False


def _finish_send_file(resp: Response, strip_etag: bool) -> Response:
    """Apply the `etag=False` strip-and-reset to a built FileResponse."""
    if strip_etag:
        resp.headers.pop(HEADER_ETAG, None)
        resp._encoded = None
    return resp


def _range_negotiable_request() -> Any:
    """Return the request a file helper may negotiate a `Range` against, if any.

    `None` while an exception handler runs: a `Range` applies only to the target
    resource's own representation (RFC 9110 Sec. 14.2), and an error page is a
    stand-in for one the request did not get.
    """
    if _serving_error_page_var.get():
        return None
    return _current_request_var.get()


def send_file(
    path_or_file: Any,
    mimetype: str | None = None,
    *,
    as_attachment: bool = False,
    download_name: str | None = None,
    last_modified: Any = None,
    etag: bool | str = True,
    max_age: int | None = None,
) -> Response:
    """Serve a file from a filesystem path.

    Accepts a `str` or `PathLike` and returns a `FileResponse` with the
    conditional-GET headers (`Last-Modified` and `ETag`) already set.

    Args:
        path_or_file: Filesystem path of the file to serve.
        mimetype: Overrides the auto-guessed content type.
        as_attachment: Send `Content-Disposition: attachment` with the
            `download_name` or the file's basename as the filename.
        download_name: Overrides the filename in `Content-Disposition`.
        last_modified: Overrides the file's mtime (datetime, unix timestamp,
            or a pre-formatted IMF-fixdate string).
        etag: `False` suppresses the generated ETag; a string is used verbatim
            and must already be quoted.
        max_age: Adds `Cache-Control: public, max-age=<n>` when set.

    Returns:
        A `FileResponse` for the file.

    """
    path, attachment_name, headers, strip_etag = _build_send_file_args(
        path_or_file, as_attachment, download_name, last_modified, etag, max_age
    )
    resp = FileResponse(
        path=path,
        filename=attachment_name,
        content_type=mimetype,
        headers=headers,
        # This helper serves the file the current request asked for, so it opts
        # into `Range` handling; a hand-built `FileResponse` does not.
        request=_range_negotiable_request(),
    )
    return _finish_send_file(resp, strip_etag)


async def async_send_file(
    path_or_file: Any,
    mimetype: str | None = None,
    *,
    as_attachment: bool = False,
    download_name: str | None = None,
    last_modified: Any = None,
    etag: bool | str = True,
    max_age: int | None = None,
) -> Response:
    """Serve a file - async variant of `send_file`.

    Reads the file in an executor, so it never blocks the event loop. Prefer it
    from async handlers; the sync `send_file` warns when called on a running loop.

    Args:
        path_or_file: Filesystem path of the file to serve.
        mimetype: Overrides the auto-guessed content type.
        as_attachment: Send `Content-Disposition: attachment`.
        download_name: Overrides the filename in `Content-Disposition`.
        last_modified: Overrides the file's mtime (datetime, unix timestamp,
            or a pre-formatted IMF-fixdate string).
        etag: `False` suppresses the generated ETag; a string is used verbatim
            and must already be quoted.
        max_age: Adds `Cache-Control: public, max-age=<n>` when set.

    Returns:
        A `FileResponse` for the file.

    """
    path, attachment_name, headers, strip_etag = _build_send_file_args(
        path_or_file, as_attachment, download_name, last_modified, etag, max_age
    )
    resp = await FileResponse.from_path(
        path=path,
        filename=attachment_name,
        content_type=mimetype,
        headers=headers,
        request=_range_negotiable_request(),
    )
    return _finish_send_file(resp, strip_etag)


# ── send_from_directory() ─────────────────────────────────


def send_from_directory(
    directory: str,
    filename: str,
    mimetype: str | None = None,
    *,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> FileResponse:
    """Send a file from a directory (sync version).

    Traversal-safe via `safe_join`. Returns 403 on any escape attempt.

    For async, use send_from_directory_async() instead.

    Args:
        directory: Root directory the file must live under.
        filename: Requested path, relative to `directory`.
        mimetype: Overrides the auto-guessed content type.
        as_attachment: Send `Content-Disposition: attachment`.
        download_name: Overrides the filename in `Content-Disposition`.

    Returns:
        A `FileResponse` for the file.

    Raises:
        Forbidden: If `filename` resolves outside `directory`.

    """  # noqa: DOC502 - the exception is raised or caught by a callee
    resolved = safe_join(directory, filename)
    if resolved is None:
        from veloce.helpers import abort  # breaks helpers <-> _helpers_files cycle

        abort(HTTP_403_FORBIDDEN, MSG_ACCESS_DENIED)

    attachment_name = _attachment_name(str(resolved), as_attachment, download_name)
    return FileResponse(
        path=resolved,
        filename=attachment_name,
        content_type=mimetype,
        request=_range_negotiable_request(),
    )


async def send_from_directory_async(
    directory: str,
    filename: str,
    mimetype: str | None = None,
    *,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> FileResponse:
    """Send a file from a directory - async version, reads file in executor.

    Traversal-safe via `safe_join`.

    Args:
        directory: Root directory the file must live under.
        filename: Requested path, relative to `directory`.
        mimetype: Overrides the auto-guessed content type.
        as_attachment: Send `Content-Disposition: attachment`.
        download_name: Overrides the filename in `Content-Disposition`.

    Returns:
        A `FileResponse` for the file.

    Raises:
        Forbidden: If `filename` resolves outside `directory`.

    """  # noqa: DOC502 - the exception is raised or caught by a callee
    # `safe_join` is pure string arithmetic; the file read happens below.
    resolved = safe_join(directory, filename)
    if resolved is None:
        from veloce.helpers import abort  # breaks helpers <-> _helpers_files cycle

        abort(HTTP_403_FORBIDDEN, MSG_ACCESS_DENIED)

    attachment_name = _attachment_name(str(resolved), as_attachment, download_name)
    return await FileResponse.from_path(
        path=resolved,
        filename=attachment_name,
        content_type=mimetype,
        request=_range_negotiable_request(),
    )
