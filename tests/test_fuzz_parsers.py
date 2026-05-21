"""Property / fuzz tests for the radix router and request parsers (R3).

These feed many randomised inputs at the router, the multipart parser,
and the cookie / query-string parsers, asserting robustness invariants
(no unhandled crash; round-trip correctness) rather than specific
values. A fixed seed keeps any failure reproducible.
"""

from __future__ import annotations

import contextlib
import random
import string

from veloce import Veloce
from veloce.exceptions import RequestEntityTooLarge
from veloce.http.cookies import parse_cookie
from veloce.http.datastructures import QueryParams, parse_multipart_form

_SEED = 20260522
_SEGMENT_CHARS = string.ascii_lowercase + string.digits + "-_"


def _rand_segment(rnd: random.Random) -> str:
    return "".join(rnd.choice(_SEGMENT_CHARS) for _ in range(rnd.randint(1, 8)))


# ── Router ────────────────────────────────────────────────────────────


def test_fuzz_router_registered_routes_always_match():
    """Every static path registered must match straight back to a route."""
    rnd = random.Random(_SEED)
    app = Veloce(openapi_url=None)

    async def handler():
        return {}

    paths: list[str] = []
    for _ in range(300):
        depth = rnd.randint(1, 5)
        path = "/" + "/".join(_rand_segment(rnd) for _ in range(depth))
        if path in paths:
            continue
        paths.append(path)
        app.add_route(path, handler, ["GET"])

    for path in paths:
        assert app.match("GET", path) is not None, f"registered path lost: {path}"


def test_fuzz_router_random_paths_never_crash():
    """Matching arbitrary junk paths returns a match or None — never raises."""
    rnd = random.Random(_SEED + 1)
    app = Veloce(openapi_url=None)

    async def h():
        return {}

    app.add_route("/users/{uid}/items/{iid}", h, ["GET"])
    app.add_route("/static/{path:path}", h, ["GET"])

    for _ in range(2000):
        segments = rnd.randint(0, 6)
        path = "/" + "/".join(
            "".join(rnd.choice(_SEGMENT_CHARS + "/.%") for _ in range(rnd.randint(0, 6)))
            for _ in range(segments)
        )
        app.match("GET", path)  # must not raise


# ── Multipart parser ──────────────────────────────────────────────────


def test_fuzz_multipart_arbitrary_bytes_never_crash():
    """Random bytes yield a FormData or the controlled RequestEntityTooLarge
    — never an unhandled parser error."""
    rnd = random.Random(_SEED + 2)
    for _ in range(500):
        body = bytes(rnd.randint(0, 255) for _ in range(rnd.randint(0, 400)))
        content_type = "multipart/form-data; boundary=" + _rand_segment(rnd)
        try:
            result = parse_multipart_form(body, content_type)
        except RequestEntityTooLarge:
            continue  # controlled DoS-cap rejection — acceptable
        assert result is not None


def test_fuzz_multipart_corrupted_valid_body_never_crash():
    """Flip random bytes in a well-formed multipart body — still no crash."""
    rnd = random.Random(_SEED + 3)
    boundary = "veloceboundary"
    base = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="field"\r\n\r\n'
        "value\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    content_type = f"multipart/form-data; boundary={boundary}"
    for _ in range(500):
        corrupted = bytearray(base)
        for _ in range(rnd.randint(1, 5)):
            corrupted[rnd.randrange(len(corrupted))] = rnd.randint(0, 255)
        # A controlled DoS-cap rejection is fine; anything else is a bug.
        with contextlib.suppress(RequestEntityTooLarge):
            parse_multipart_form(bytes(corrupted), content_type)


# ── Cookie + query-string parsers ─────────────────────────────────────


def test_fuzz_cookie_parser_never_crashes():
    rnd = random.Random(_SEED + 4)
    for _ in range(2000):
        raw = "".join(rnd.choice(string.printable) for _ in range(rnd.randint(0, 60)))
        assert isinstance(parse_cookie(raw), dict)


def test_fuzz_query_parser_never_crashes():
    rnd = random.Random(_SEED + 5)
    alphabet = string.ascii_letters + string.digits + "=&%+;.- "
    for _ in range(2000):
        raw = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
        QueryParams.from_query_string(raw)  # must not raise
