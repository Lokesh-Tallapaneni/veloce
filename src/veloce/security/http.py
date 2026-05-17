"""HTTP Basic and Bearer authentication schemes."""

from __future__ import annotations

import base64

from veloce.exceptions import HTTPException
from veloce.http.request import Request


class HTTPBasicCredentials:
    """HTTP Basic auth credentials."""

    __slots__ = ("username", "password")

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password


class HTTPBasic:
    """HTTP Basic authentication — extracts username:password from Authorization header."""

    def __init__(self, auto_error: bool = True, realm: str = "") -> None:
        self.auto_error = auto_error
        self.realm = realm

    def __call__(self, request: Request) -> HTTPBasicCredentials | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            if self.auto_error:
                headers: dict[str, str] = {}
                if self.realm:
                    headers["WWW-Authenticate"] = f'Basic realm="{self.realm}"'
                raise HTTPException(401, "Not authenticated", headers=headers)
            return None

        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
            return HTTPBasicCredentials(username=username, password=password)
        except Exception as err:
            raise HTTPException(401, "Invalid authentication credentials") from err


class HTTPDigestCredentials:
    """Parsed Digest auth challenge response — RFC 7616 §3.4."""

    __slots__ = (
        "username",
        "realm",
        "nonce",
        "uri",
        "response",
        "qop",
        "nc",
        "cnonce",
        "opaque",
        "algorithm",
    )

    def __init__(
        self,
        username: str = "",
        realm: str = "",
        nonce: str = "",
        uri: str = "",
        response: str = "",
        qop: str = "",
        nc: str = "",
        cnonce: str = "",
        opaque: str = "",
        algorithm: str = "",
    ) -> None:
        self.username = username
        self.realm = realm
        self.nonce = nonce
        self.uri = uri
        self.response = response
        self.qop = qop
        self.nc = nc
        self.cnonce = cnonce
        self.opaque = opaque
        self.algorithm = algorithm


class HTTPDigest:
    """HTTP Digest authentication — RFC 7616.

    Parses the `Authorization: Digest …` header into the named fields
    and returns them as `HTTPDigestCredentials`. **This class does NOT
    validate the response hash** — the application owns the secret
    (HA1) and must compute the expected digest itself; Digest's whole
    point is that the secret never crosses the wire. Veloce's job is to
    parse the challenge response and to emit a 401 + `WWW-Authenticate:
    Digest …` header when auth is missing or malformed.

    The scheme's responsibility is the parse + challenge dance;
    verifying the response is application logic.
    """

    def __init__(
        self,
        realm: str,
        qop: str = "auth",
        algorithm: str = "MD5",
        auto_error: bool = True,
        nonce_factory: Any = None,
    ) -> None:
        self.realm = realm
        self.qop = qop
        self.algorithm = algorithm
        self.auto_error = auto_error
        self.nonce_factory = nonce_factory or _default_nonce

    def _challenge_headers(self) -> dict[str, str]:
        from urllib.parse import quote

        nonce = self.nonce_factory()
        # RFC 7616 §3.3 — challenge param names case-insensitive but
        # the quoted-string values must be exact. Build the header
        # rigorously; clients in the wild reject malformed challenges.
        parts = [
            f'realm="{quote(self.realm)}"',
            f'qop="{self.qop}"',
            f'nonce="{nonce}"',
            f"algorithm={self.algorithm}",
        ]
        return {"WWW-Authenticate": "Digest " + ", ".join(parts)}

    def __call__(self, request: Request) -> HTTPDigestCredentials | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Digest "):
            if self.auto_error:
                raise HTTPException(
                    401,
                    "Not authenticated",
                    headers=self._challenge_headers(),
                )
            return None
        return _parse_digest(auth[7:])


def _default_nonce() -> str:
    """Generate an opaque nonce for the Digest challenge.

    16 random bytes hex-encoded — well above the 64-bit entropy floor
    RFC 7616 §5.3 recommends. Server-side nonce replay tracking is
    application territory.
    """
    import secrets

    return secrets.token_hex(16)


def _parse_digest(value: str) -> HTTPDigestCredentials:
    """Split a `key=value, key="quoted value"` Digest field list.

    RFC 7616 §3.4 — the field set is open-ended, so we collect every
    pair and assign known names to the credential's slots. Unknown
    fields are ignored (e.g. `userhash=true` extensions). Quoted
    values are unwrapped; unquoted values pass through verbatim.
    """
    fields: dict[str, str] = {}
    i = 0
    while i < len(value):
        eq = value.find("=", i)
        if eq == -1:
            break
        key = value[i:eq].strip().lower()
        j = eq + 1
        if j < len(value) and value[j] == '"':
            # Quoted string — walk to the matching close quote, honouring
            # backslash escapes inside (rare but legal per RFC 7616).
            end = j + 1
            while end < len(value):
                if value[end] == "\\" and end + 1 < len(value):
                    end += 2
                    continue
                if value[end] == '"':
                    break
                end += 1
            val = value[j + 1 : end].replace('\\"', '"').replace("\\\\", "\\")
            i = end + 1
        else:
            end = value.find(",", j)
            if end == -1:
                end = len(value)
            val = value[j:end].strip()
            i = end
        fields[key] = val
        # Skip the trailing comma + whitespace.
        while i < len(value) and value[i] in (",", " ", "\t"):
            i += 1

    return HTTPDigestCredentials(
        username=fields.get("username", ""),
        realm=fields.get("realm", ""),
        nonce=fields.get("nonce", ""),
        uri=fields.get("uri", ""),
        response=fields.get("response", ""),
        qop=fields.get("qop", ""),
        nc=fields.get("nc", ""),
        cnonce=fields.get("cnonce", ""),
        opaque=fields.get("opaque", ""),
        algorithm=fields.get("algorithm", ""),
    )


from typing import Any  # noqa: E402 — placed late to keep public symbols at top


class HTTPBearer:
    """HTTP Bearer token authentication."""

    def __init__(self, auto_error: bool = True, scheme_name: str = "Bearer") -> None:
        self.auto_error = auto_error
        self.scheme_name = scheme_name

    def __call__(self, request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith(f"{self.scheme_name} "):
            if self.auto_error:
                raise HTTPException(
                    401,
                    "Not authenticated",
                    headers={"WWW-Authenticate": self.scheme_name},
                )
            return None
        return auth[len(self.scheme_name) + 1 :]
