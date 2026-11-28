"""Hooks and route resolution for the dispatch pipeline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import veloce.status as status
from veloce._constants import (
    HEADER_ALLOW,
    HEADER_HOST,
)
from veloce._internal import (
    _extract_host,
)
from veloce._pipeline import (
    CompiledPipeline,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    HTTP_METHOD_OPTIONS,
)
from veloce.app._host import AppHost
from veloce.app.dispatch_support import _adapt_hook_kwargs, _after_hook_sig_cache
from veloce.blueprints import _endpoint_blueprint
from veloce.exceptions import (
    MethodNotAllowed,
    NotFound,
)
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    RedirectResponse,
    Response,
)


class HookDispatchMixin(AppHost):
    """Before/after hooks, URL value preprocessors and route resolution."""

    async def _run_before_hooks(self, request: Request) -> tuple[Response | None, str | None]:
        """Run before_request hooks; return `(short_circuit_response, bp_name)`.

        The blueprint bucket is selected from `request.endpoint` *after* the
        app-level hooks run, so a hook that rewrites the endpoint is honoured.
        `bp_name` is the teardown blueprint, so a short-circuit inside an
        app-level hook means no blueprint teardown.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                response = await self._run_response_middleware(
                    request, self._coerce_response(result)
                )
                return response, None
        bp_name = _endpoint_blueprint(request.endpoint)
        if self._bp_before_hooks and bp_name is not None:
            for hook in self._bp_before_hooks.get(bp_name, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    response = await self._run_response_middleware(
                        request, self._coerce_response(result)
                    )
                    return response, bp_name
        return None, bp_name

    async def _resolve_route(
        self,
        request: Request,
        match: Any,
        matched_path: str,
        matched_method: str,
        cp: CompiledPipeline,
        excluded: bool,
    ) -> Any:
        """Resolve the route to dispatch, or a terminal Response.

        Returns either a Response (already through response middleware) or the
        match to dispatch, having populated `path_params`, defaults, endpoint
        and url_rule and run the URL value preprocessors. Raises `HTTPException`
        for the 404 / constraint-mismatch cases.
        """
        # A linear scan, kept deliberately: ~0.07 us per mount per request (min-of-7
        # over 5k each: 0 mounts 5.54 us, 3 mounts 6.91 us, 50 mounts 10.14 us).
        if cp.has_mounted_apps:
            for prefix, prefix_slash, sub_app in self._mounted_apps:
                if request.path.startswith(prefix_slash) or request.path == prefix:
                    sub_path = request.path[len(prefix) :] or "/"
                    # Only the sub-app knows whether its route streams.
                    sub_match = sub_app.match(request.method, sub_path)
                    streams = sub_match is not None and sub_match.route_info.stream
                    # Stacks under the parent's root_path when it is mounted too.
                    sub_request = Request.derive_for_mount(
                        request,
                        sub_path,
                        b"" if streams else await request.body(),
                        sub_app,
                        prefix,
                        body_source=request._body_source if streams else None,
                    )
                    response = await sub_app.handle_request(sub_request)
                    return await self._run_response_middleware(request, response)

        if cp.has_static_handlers:
            for static in self._static_handlers:
                response = await static.handle(request)
                if response is not None:
                    return await self._run_response_middleware(request, response)

        if request.path != matched_path or request.method != matched_method:
            match = self.match(request.method, request.path)

        # A mismatch is 404, not 405: the path is reachable, not from this host.
        if (
            match is not None
            and match.route_info.subdomain is not None
            and not self._subdomain_matches(request, match.route_info.subdomain)
        ):
            raise NotFound()

        # Case-insensitive and port-stripped; a mismatch is likewise a 404.
        if match is not None and match.route_info.host is not None:
            req_host = _extract_host(request.headers.get(HEADER_HOST, "") or "")
            if req_host != match.route_info.host:
                raise NotFound()

        if match is None and self.redirect_slashes:
            alt = (
                request.path.rstrip("/")
                if request.path.endswith("/") and request.path != "/"
                else request.path + "/"
            )
            alt_match = self.match(request.method, alt)
            if alt_match is not None:
                code = (
                    status.HTTP_308_PERMANENT_REDIRECT
                    if request.method != HTTP_METHOD_GET
                    else status.HTTP_307_TEMPORARY_REDIRECT
                )
                # `alt` is mount-local, so `Location` must carry `root_path`.
                target = request.root_path + alt
                if target.startswith("//"):
                    # `//` is protocol-relative: the browser reads what follows
                    # as a host, so a reflected path is an open redirect.
                    target = "/" + target.lstrip("/")
                response = RedirectResponse(target, status_code=code)
                response = await self._run_response_phase(cp.http_post, request, response, excluded)
                return response

        if match is None:
            allowed = self.get_allowed_methods(request.path)
            if allowed:
                # RFC 9110 Sec. 9.3.7: OPTIONS answers `Allow:` with an empty body.
                if request.method == HTTP_METHOD_OPTIONS:
                    response = self.make_default_options_response(
                        request.path, allowed_methods=allowed
                    )
                    response = await self._run_response_phase(
                        cp.http_post, request, response, excluded
                    )
                    return response
                # RFC 9110 Sec. 15.5.6: a 405 names the methods it does serve, so
                # `Allow` rides on the exception and the body carries `allowed`.
                exc_405 = MethodNotAllowed(headers={HEADER_ALLOW: ", ".join(allowed)})
                return await self._handle_error(
                    request,
                    exc_405.status_code,
                    JSONResponse(
                        {"detail": exc_405.detail, "allowed": allowed},
                        status_code=exc_405.status_code,
                        headers=exc_405.headers,
                    ),
                    exc_405,
                )
            raise NotFound()

        request.path_params = match.path_params
        if match.route_info.defaults:
            for _dk, _dv in match.route_info.defaults.items():
                request.path_params.setdefault(_dk, _dv)
        request.endpoint = match.route_info.name
        request._state["url_rule"] = match.route_info.path_template

        # Inline so a route with none pays no frame; MCP uses the helper below.
        if self._url_value_preprocessors:
            endpoint = match.route_info.name
            for proc in self._url_value_preprocessors:
                proc(endpoint, request.path_params)
        if self._bp_url_value_preprocessors:
            endpoint = match.route_info.name
            bp = _endpoint_blueprint(endpoint)
            bp_procs = self._bp_url_value_preprocessors.get(bp) if bp is not None else None
            if bp_procs is not None:
                for proc in bp_procs:
                    proc(endpoint, request.path_params)

        return match

    def _run_url_value_preprocessors(
        self, endpoint: str | None, path_params: dict[str, Any]
    ) -> None:
        """Run every registered `url_value_preprocessor` against `path_params`.

        Each receives `(endpoint, path_params)` and may mutate it in place.
        App-level processors run first, then the endpoint's blueprint's.
        """
        if self._url_value_preprocessors:
            for proc in self._url_value_preprocessors:
                proc(endpoint, path_params)
        if self._bp_url_value_preprocessors:
            bp = _endpoint_blueprint(endpoint)
            bp_procs = self._bp_url_value_preprocessors.get(bp) if bp is not None else None
            if bp_procs is not None:
                for proc in bp_procs:
                    proc(endpoint, path_params)

    async def _run_after_hooks(
        self, request: Request, response: Response, bp_name: str | None
    ) -> Response:
        """Run after_request hooks and one-shot `after_this_request` callbacks.

        Reverse registration order, and each may return a replacement Response.
        """
        for hook in reversed(self._after_request_hooks):
            hook_result = await self._call_after_hook(hook, request, response)
            if isinstance(hook_result, Response):
                response = hook_result
        if self._bp_after_hooks and bp_name is not None:
            for hook in reversed(self._bp_after_hooks.get(bp_name, ())):
                hook_result = await self._call_after_hook(hook, request, response)
                if isinstance(hook_result, Response):
                    response = hook_result

        # After the global hooks, so a per-request adjustment sees their work.
        one_shot = request._state.get("_after_this_request") if request._state else None
        if one_shot:
            for fn in one_shot:
                fn_result = await self._call_after_hook(fn, request, response)
                if isinstance(fn_result, Response):
                    response = fn_result
        return response

    def _subdomain_matches(self, request: Request, subdomain: str) -> bool:
        """Check whether `request`'s host carries the expected subdomain.

        `"*"` matches any non-empty subdomain. The request's own subdomain comes
        from `Request.subdomain`, which short-circuits an IP literal - a second
        derivation would match `subdomain="192"` against `192.168.1.1`.
        """
        actual = request.subdomain
        if subdomain == "*":
            return bool(actual)
        return actual == subdomain

    async def _call_after_hook(
        self, hook: Callable[..., Any], request: Request, response: Response
    ) -> Any:
        """Call an after-request hook, adapting kwargs to match its signature."""
        kwargs = _adapt_hook_kwargs(hook, _after_hook_sig_cache, "response", request, response)
        return await self._call_handler(hook, kwargs)
