"""Request URL — the URL, matched route, reverse building and the client connection."""

from __future__ import annotations

from typing import Any

from veloce._constants import (
    HEADER_HOST,
    HEADER_X_FORWARDED_FOR,
)
from veloce._internal import (
    _extract_host,
)
from veloce._protocol_constants import SECURE_URL_SCHEMES, URL_SCHEME_HTTPS
from veloce.http.datastructures import (
    URL,
    Address,
)
from veloce.http.request._negotiation import NegotiationMixin


class UrlMixin(NegotiationMixin):
    """URL, routing and connection accessors."""

    __slots__ = ()

    # ── URL and routing ───────────────────────────────────
    @property
    def url(self) -> URL:
        """Full URL object - lazy construction."""
        if self._url is None:
            scope_scheme = self.scope.get("scheme")
            # The raw transport has no ASGI scope to carry TLS, and outranks the
            # `X-Forwarded-Proto` below: no header beats the socket it arrived on.
            if (
                scope_scheme is None
                and self.transport is not None
                and self.transport.get_extra_info("ssl_object") is not None
            ):
                scope_scheme = URL_SCHEME_HTTPS
            # A trusted ProxyFix hop stashes the public port here.
            state = self._state
            forwarded_port = state.get("proxy_fix_port") if state else None
            self._url = URL.from_request(
                self.headers,
                self.path,
                self.query_string,
                scope_scheme=scope_scheme,
                forwarded_port=forwarded_port,
                trust_forwarded_proto=not (state and "proxy_fix_applied" in state),
            )
        return self._url

    @property
    def base_url(self) -> str:
        """Base URL (scheme + host)."""
        url = self.url
        return f"{url.scheme}://{url.netloc}"

    @property
    def full_path(self) -> str:
        """Path + `?` + query string.

        Always contains a `?`, even when the query string is empty.
        """
        return f"{self.path}?{self.query_string}"

    @property
    def url_root(self) -> str:
        """Root URL of the request: `scheme://host/`.

        Carries the trailing slash, and no path or query string.
        """
        url = self.url
        return f"{url.scheme}://{url.netloc}/"

    @property
    def host_url(self) -> str:
        """Alias for `url_root` - Veloce exposes both names."""
        return self.url_root

    @property
    def is_secure(self) -> bool:
        """Return True if the connection is encrypted (`https` or `wss`)."""
        return self.url.scheme in SECURE_URL_SCHEMES

    @property
    def scheme(self) -> str:
        """Request scheme - `"http"` or `"https"`.

        Sourced from the ASGI `scope["scheme"]` when present, then from
        the `X-Forwarded-Proto` header (only meaningful behind a trusted
        proxy), then default `http`.
        """
        return self.url.scheme

    @property
    def host(self) -> str:
        """Value of the `Host` request header.

        Mirrors `Request.url.netloc` for the common case but pulls
        directly from the header to remain cheap (no full URL parse).
        Returns the empty string when the header is absent.
        """
        return self.headers.get(HEADER_HOST, "")

    @property
    def root_path(self) -> str:
        """The URL prefix the app is mounted under.

        Comes from the ASGI server (e.g. uvicorn `--root-path /api`) or from
        `app.mount("/sub", inner_app)`, and falls back to `Veloce(root_path=...)`
        when neither supplies one. That constructor argument is documented as a
        way to set the prefix and was read by nothing, so an app configured that
        way built unprefixed external URLs and unprefixed slash redirects.

        The server wins when it sets one: it knows where the app was actually
        mounted, and the constructor argument is a declaration made before that
        is known. The built-in server sets none, which is why the fallback
        matters there.

        Returns the empty string when the app is at root.
        """
        scope_root = self.scope.get("root_path", "")
        if scope_root:
            return scope_root
        app = self.app
        return app.root_path if app is not None else ""

    @property
    def script_root(self) -> str:
        """Alias for `root_path` — also called `script_root`.

        ProxyFix-style middleware may also set
        `_state["proxy_fix_prefix"]`; that wins over the ASGI scope
        because it represents the *trusted* outer-edge prefix when the
        ASGI server is behind a reverse proxy that strips the prefix.
        """
        proxied = self._state.get("proxy_fix_prefix")
        if proxied:
            return proxied
        return self.root_path

    @property
    def subdomain(self) -> str:
        """Leftmost host label minus `app.config["SERVER_NAME"]`.

        Returns the empty string when the request host equals `SERVER_NAME`
        exactly (apex), when `SERVER_NAME` is not configured and the host has no
        dots, or when the host is an IP literal.

        Cached: a subdomain-routed request asks twice - once by the router to
        decide the match, once by the handler to read the value - and it is
        derived from request data that cannot change. The router asks through
        this property rather than deriving its own answer, because the two used
        to disagree.
        """
        cached = self._subdomain
        if cached is not None:
            return cached
        # Inline: measurable on every subdomain-routed match. `_extract_host`
        # handles an IPv6 literal, whose first colon is not a port separator.
        host = _extract_host(self.host or "")
        if not host:
            self._subdomain = ""
            return ""
        # An IP literal has no subdomain: its dots are address structure. The
        # allocating all-digits test sits behind a cheap first-character check.
        if ":" in host or (host[0].isdigit() and host.replace(".", "").isdigit()):
            self._subdomain = ""
            return ""
        app = self.app
        cfg = app.config if app is not None else None
        server_name = ((cfg.get("SERVER_NAME") if cfg else "") or "").lower()
        if server_name:
            if host.endswith("." + server_name):
                self._subdomain = value = host[: -(len(server_name) + 1)]
                return value
            self._subdomain = ""
            return ""
        # No SERVER_NAME: the leftmost label, unless that is the apex itself.
        if "." not in host:
            self._subdomain = ""
            return ""
        self._subdomain = value = host.split(".", 1)[0]
        return value

    @property
    def environ(self) -> dict[str, Any]:
        """Alias for the ASGI `scope` dict.

        Third-party code paths reach for `request.environ` (WSGI); ASGI
        scope is the analogue. Returns the live dict so middleware can
        introspect (mutation goes through framework APIs, not this).
        """
        return self.scope

    @property
    def url_rule(self) -> str | None:
        """Return the matched route's template (e.g. `/users/{id}`).

        Returns the raw path template the radix tree used for the match —
        i.e. `path_params` placeholders are unsubstituted. `None` for
        synthetic requests that never went through dispatch.
        """
        return self._state.get("url_rule") if self._state else None

    @property
    def is_mcp(self) -> bool:
        """Return whether this request is a replayed MCP tool / resource call.

        `True` when the request was synthesised by the MCP integration to replay a
        route through `Depends` / middleware for an agent call, rather than a real
        HTTP request. Authentication middleware that checks a browser credential
        (a session cookie, an `Authorization` header) should return early on these
        - the MCP transport authenticates the agent separately. The transport
        request itself (`POST /mcp`) opts such middleware out via
        `mount_mcp(..., exclude_middleware=[...])`.
        """
        return bool(self._state.get("_mcp")) if self._state else False

    @property
    def blueprint(self) -> str | None:
        """Return the name of the blueprint that owns the matched route.

        Veloce stores the endpoint as `<bp>.<name>` for blueprint routes.
        Returns the bit before the dot, or `None` if the endpoint is
        unset or is a top-level (no-dot) name.
        """
        ep = self.endpoint
        if not ep or "." not in ep:
            return None
        return ep.rsplit(".", 1)[0]

    @property
    def blueprints(self) -> list[str]:
        """Return every blueprint in the matched endpoint's parent chain.

        For an endpoint `a.b.c.view`, returns `["a.b.c", "a.b", "a"]`
        (innermost first). Empty list when the route is top-level or the
        endpoint is unset.
        """
        ep = self.endpoint
        if not ep or "." not in ep:
            return []
        parts = ep.rsplit(".", 1)[0].split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1)]

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """Reverse-resolve a route URL - ASGI shape.

        `request.url_for("route_name", id=7)` delegates to the bound app's
        `url_for`. Raises `RuntimeError` when the request has no app bound
        (synthetic requests built outside dispatch).

        With `_external=True` the absolute URL is built from *this request's*
        origin - the scheme, host and port a trusted `ProxyFix` recovered - and
        carries `script_root`, so a link generated behind a proxy that
        terminates TLS on another port and mounts the app under a prefix points
        at the public URL rather than the internal one. `app.url_for` has no
        request to read and falls back to `SERVER_NAME`. An explicit `_scheme`
        or `_host` still wins.
        """
        if self.app is None:
            raise RuntimeError(
                "Request.url_for requires a bound app; this request was "
                "constructed outside the dispatch pipeline"
            )
        if not path_params.pop("_external", False):
            return self.app.url_for(name, **path_params)
        scheme = path_params.pop("_scheme", None)
        host = path_params.pop("_host", None)
        # The app builds the path; the origin and mount prefix come from here.
        path = self.app.url_for(name, **path_params)
        url = self.url
        root = self.script_root.rstrip("/")
        return f"{scheme or url.scheme}://{host or url.netloc}{root}{path}"

    # ── Client and connection ─────────────────────────────
    @property
    def client_host(self) -> str | None:
        """Return the client's IP address, or `None` when the peer is unknown."""
        # A ProxyFix-style middleware stashes the trusted IP on `_state`.
        proxied = self._state.get("proxy_fix_client")
        if proxied:
            return proxied
        if self.transport:
            peername = self.transport.get_extra_info("peername")
            if peername:
                return peername[0]
        # On ASGI the peer lives on the scope; `transport` is always None there.
        client = self.scope.get("client") if self.scope else None
        return client[0] if client else None

    @property
    def client_port(self) -> int | None:
        """Return the client's port number, or `None` when the peer is unknown."""
        if self.transport:
            peername = self.transport.get_extra_info("peername")
            if peername and len(peername) >= 2:
                return peername[1]
        client = self.scope.get("client") if self.scope else None
        return client[1] if client and len(client) >= 2 else None

    @property
    def client(self) -> Address | None:
        """The connecting peer as an `Address(host, port)`.

        `request.client.host` / `request.client.port` work, and tuple
        unpacking (`host, port = request.client`) works too. Returns
        `None` when the peer is unknown (e.g. synthetic requests).
        Honours ProxyFix - `client.host` reflects the trusted client IP.
        """
        host = self.client_host
        if host is None:
            return None
        return Address(host, self.client_port or 0)

    @property
    def remote_addr(self) -> str | None:
        """Alias for `client_host` — the connecting client's IP.

        Honours ProxyFix-style middleware: when the trusted hop has set
        `_state["proxy_fix_client"]`, that value wins over the raw TCP
        peer (the ASGI/uvicorn `client[0]` may be the load balancer).
        """
        return self.client_host

    @property
    def access_route(self) -> list[str]:
        """Forwarded-for chain.

        Returns the comma-separated `X-Forwarded-For` values (client ->
        proxy chain order), with the connecting peer (`remote_addr`)
        appended at the end. With no `X-Forwarded-For` header, returns
        `[remote_addr]` when the peer is known, else `[]`.

        RFC 7239 Sec. 5.2 defines the IP-order convention: leftmost is the
        originating client, rightmost is the closest proxy. Production
        code should consume the leftmost *trusted* entry, not blindly
        the leftmost value.
        """
        forwarded = self.headers.get(HEADER_X_FORWARDED_FOR, "")
        chain: list[str] = [v.strip() for v in forwarded.split(",") if v.strip()]
        peer = self.client_host
        if peer:
            chain.append(peer)
        return chain
