"""One table of `Response` header accessors, shared by the two modules on them.

`test_response_header_casing.py` (read side) and
`test_response_header_clearing.py` (clear side) each kept their own nine-row
copy, in different orders and with different rows: the read table had
`content_range`, the clear table had `date`, and neither reached the other's.
A property could therefore be covered for reading and not for clearing, or the
reverse, with nothing to say which.

The date-valued accessors are listed separately because they do not read back
as strings - `response.date` is a `datetime` - so the read assertions differ
even though the clear assertions do not.
"""

from __future__ import annotations

DATE_VALUE = "Wed, 21 Oct 2015 07:28:00 GMT"

#: `(accessor, canonical header, stored value, expected read)` for the
#: accessors that read back the stored string unchanged.
STRING_ACCESSORS: list[tuple[str, str, str, object]] = [
    ("www_authenticate", "WWW-Authenticate", 'Bearer realm="api"', 'Bearer realm="api"'),
    ("content_encoding", "Content-Encoding", "gzip", "gzip"),
    ("content_language", "Content-Language", "en", "en"),
    ("accept_ranges", "Accept-Ranges", "bytes", "bytes"),
    ("content_range", "Content-Range", "bytes 0-1/2", "bytes 0-1/2"),
    ("location", "Location", "/next", "/next"),
    ("content_location", "Content-Location", "/here", "/here"),
    ("age", "Age", "12", 12),
    ("retry_after", "Retry-After", "30", 30),
]

#: `(accessor, canonical header)` for the accessors that parse to a `datetime`.
DATE_ACCESSORS: list[tuple[str, str]] = [
    ("date", "Date"),
    ("expires", "Expires"),
    ("last_modified", "Last-Modified"),
]


def _spelling(header: str, index: int) -> str:
    """A non-canonical spelling of `header`, varied across the table.

    Rotated rather than hand-listed: the point is that clearing works whatever
    casing wrote the header, and a hand-maintained column drifts row by row.
    """
    return (header.lower(), header.upper(), header.capitalize())[index % 3]


#: `content_range` is the one accessor with no setter, so it can be read but
#: not written or cleared. That is why the two hand-maintained tables differed:
#: the read table had it and the clear table did not, which read as drift and
#: is actually the API. Stated here once rather than implied by an omission.
READ_ONLY = frozenset({"content_range"})

#: Every settable accessor, as `(accessor, canonical header, a value, a
#: non-canonical spelling)` - what the clear side needs, where the read type
#: does not matter.
CLEARABLE: list[tuple[str, str, str, str]] = [
    (attr, header, value, _spelling(header, index))
    for index, (attr, header, value) in enumerate(
        [
            *(
                (attr, header, stored)
                for attr, header, stored, _ in STRING_ACCESSORS
                if attr not in READ_ONLY
            ),
            *((attr, header, DATE_VALUE) for attr, header in DATE_ACCESSORS),
        ]
    )
]

#: Ids for the clear-side parametrizations.
CLEARABLE_IDS: list[str] = [row[0] for row in CLEARABLE]
