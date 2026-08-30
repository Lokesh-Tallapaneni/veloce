"""Compile a handler's parameter resolver, the way the framework does.

One line, written three times: twice in `test_resolver_codegen.py` under two
names (`_compile` and `_resolver_of`) and once in
`test_resolver_inlined_coercion.py`. The three arguments are what the resolver
is built from, so a change to any of them had three call sites to find.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from veloce._handler_plan import build_plan
from veloce._resolver_codegen import compile_param_resolver
from veloce.dependency import _coerce_value
from veloce.exceptions import RequestValidationError


def resolver_for(handler: Callable[..., Any]) -> Any:
    """The compiled resolver for `handler`, or `None` when it does not compile."""
    return compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)
