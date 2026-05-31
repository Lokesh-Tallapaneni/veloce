"""Class-based views — `View` base + `MethodView`.

Two class-based view styles:

`View` — one class, one `dispatch_request`. Override it to handle the
URL however you like (read `request.method` yourself if needed):

    class IndexView(View):
        async def dispatch_request(self, request):
            return {"page": "index"}

    app.add_url_rule("/", view_func=IndexView.as_view("index"))

`MethodView` — one async method per HTTP verb, dispatched by method:

    class UserView(MethodView):
        async def get(self, request, id: int): ...
        async def post(self, request, id: int): ...

    app.add_url_rule("/users/{id:int}", view_func=UserView.as_view("user"))

`as_view(name)` returns an async dispatcher. By default a fresh
instance is built per request (`init_every_request = True`); set it `False` to reuse a single instance. The
`decorators` class list is applied to the generated view function
outermost-last.

Veloce-specific: handlers must be `async def` — a sync verb method on
a `MethodView` subclass raises at class-definition time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from veloce._internal import _is_async_callable
from veloce.exceptions import MethodNotAllowed

# Standard HTTP method names (RFC 9110 §9.3). Lower-cased because that's
# how methods are spelled on the class; we upper-case for the Allow header.
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


class View:
    """Base class-based view — one `dispatch_request` per class.

    Subclasses override `dispatch_request`. Class attributes:

    - `methods` — the HTTP verbs this view answers (advisory; used by
      the router / OpenAPI introspection).
    - `decorators` — decorators applied to the generated view function,
      innermost-first (the last entry wraps outermost).
    - `init_every_request` — when True (default) a fresh instance is
      built for each request; when False one instance is reused.
    """

    methods: ClassVar[list[str] | None] = None
    # Shared mutable default: subclasses that want their own decorator chain
    # must assign a fresh list (`decorators = [auth]`) rather than mutating
    # this one in place, or every view class would share the same list.
    decorators: ClassVar[list[Callable]] = []
    init_every_request: ClassVar[bool] = True

    @classmethod
    def as_view(cls, name: str, *class_args: Any, **class_kwargs: Any) -> Callable:
        """Build a view function bound to this class.

        Honours `init_every_request` (fresh instance per request vs a
        single shared one) and applies `decorators`. The returned
        callable carries `view_class`, `methods`, and `__name__ = name`
        for router introspection and `url_for` naming.
        """
        if cls.init_every_request:

            async def view(request: Any, **path_params: Any) -> Any:
                instance = cls(*class_args, **class_kwargs)
                return await instance.dispatch_request(request, **path_params)

        else:
            shared = cls(*class_args, **class_kwargs)

            async def view(request: Any, **path_params: Any) -> Any:
                return await shared.dispatch_request(request, **path_params)

        view.__name__ = name
        view.view_class = cls  # type: ignore[attr-defined]
        view.methods = cls._allowed_methods()  # type: ignore[attr-defined]

        for decorator in cls.decorators:
            view = decorator(view)
        view.__name__ = name
        return view

    @classmethod
    def _allowed_methods(cls) -> list[str]:
        """The HTTP verbs this view advertises. Base `View` uses `methods`."""
        if cls.methods is not None:
            return [m.upper() for m in cls.methods]
        return ["GET"]

    async def dispatch_request(self, *args: Any, **kwargs: Any) -> Any:
        """Handle the request — subclasses must override."""
        raise NotImplementedError(f"{type(self).__name__} must implement dispatch_request()")


class MethodView(View):
    """Class-based view dispatching one async method per HTTP verb.

    Subclasses define `get` / `post` / … as `async def`. `methods` is
    inferred from the defined verbs unless set explicitly.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in _HTTP_METHODS:
            attr = getattr(cls, name, None)
            if attr is None:
                continue
            if not _is_async_callable(attr):
                raise TypeError(
                    f"{cls.__name__}.{name} must be async (Veloce handlers are `async def`-only)"
                )

    @classmethod
    def _allowed_methods(cls) -> list[str]:
        if cls.methods is not None:
            return [m.upper() for m in cls.methods]
        return [m.upper() for m in _HTTP_METHODS if getattr(cls, m, None) is not None]

    async def dispatch_request(self, *args: Any, **kwargs: Any) -> Any:
        """Pick the matching method by request verb and forward arguments.

        The first positional argument is expected to be the `Request`;
        the rest are path parameters. If the class doesn't implement
        the verb, raises `MethodNotAllowed` with `Allow:` set.
        """
        request = args[0] if args else kwargs.get("request")
        method = request.method.lower() if request is not None else "get"
        handler = getattr(self, method, None)
        if handler is None:
            raise MethodNotAllowed(
                detail=f"Method {method.upper()} not allowed",
                headers={"Allow": ", ".join(self._allowed_methods())},
            )
        return await handler(*args, **kwargs)
