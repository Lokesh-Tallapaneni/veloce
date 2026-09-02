"""One unresolvable annotation must not disarm the rest of the signature.

`get_type_hints` resolves a whole signature or none of it. The plan builder
caught the failure and returned `{}`, so a single unresolvable annotation - a
`TYPE_CHECKING`-only import under `from __future__ import annotations`, a
forward reference, a typo - erased **every** annotation on that handler, and
with them the PEP 593 metadata that carries `Depends()` and `Security()`.

The route then served unauthenticated. Two handlers declaring the same security
dependency and differing only in an unrelated parameter's annotation answered
401 and 200 respectively, with nothing logged.

Annotations are now resolved one at a time when the whole-signature pass fails,
so a broken annotation costs only its own parameter, and the loss is warned
about rather than silent.
"""

from __future__ import annotations

import warnings
from typing import Annotated

import pytest

from veloce import Depends, Security, Veloce
from veloce._handler_plan import K_QUERY, K_REQUEST, build_plan
from veloce.security.http import HTTPBearer
from veloce.testclient import TestClient

# Module scope on purpose. This module uses `from __future__ import
# annotations`, so every annotation is a string resolved against these globals;
# a scheme built inside a test function could never be found there, which is a
# PEP 563 property rather than anything about the defect under test.
BEARER = HTTPBearer(auto_error=True)


# ── the bypass ───────────────────────────────────────────────────────


def test_a_broken_sibling_annotation_does_not_disable_authentication():
    """The defect: this route answered 200 without credentials."""
    app = Veloce(openapi_url=None)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        @app.get("/guarded")
        async def guarded(
            cred: Annotated[object, Depends(BEARER)] = None,
            x: NoSuchTypeAnywhere = 1,  # noqa: F821 - deliberately unresolvable
        ) -> dict:
            return {"ok": True}

    assert TestClient(app).get("/guarded").status_code == 401


def test_the_same_handler_without_the_broken_annotation_also_enforces():
    """The control: proves the 401 above is not incidental."""
    app = Veloce(openapi_url=None)

    @app.get("/guarded")
    async def guarded(cred: Annotated[object, Depends(BEARER)] = None, x: int = 1) -> dict:
        return {"ok": True}

    assert TestClient(app).get("/guarded").status_code == 401


def test_a_valid_credential_is_still_accepted():
    """Salvage must not break the success path."""
    app = Veloce(openapi_url=None)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        @app.get("/guarded")
        async def guarded(
            cred: Annotated[object, Depends(BEARER)] = None,
            x: NoSuchTypeAnywhere = 1,  # noqa: F821
        ) -> dict:
            return {"ok": True}

    response = TestClient(app).get("/guarded", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200


# ── the salvage itself ───────────────────────────────────────────────


def test_the_resolvable_annotations_survive():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        def handler(
            request,
            good: int = 1,
            bad: StillMissing = 2,  # noqa: F821
        ) -> None:
            return None

        plan = build_plan(handler)

    kinds = {slot.name: slot.kind for slot in plan.slots}
    assert kinds["request"] == K_REQUEST
    types = {slot.name: slot.target_type for slot in plan.slots}
    assert types["good"] is int, "a resolvable annotation was lost with the broken one"


def test_the_broken_parameter_falls_back_to_the_unannotated_shape():
    """It still degrades - just only itself."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        def handler(bad: StillMissing = 2) -> None:  # noqa: F821
            return None

        plan = build_plan(handler)

    assert plan.slots[0].kind == K_QUERY
    assert plan.slots[0].target_type is str


def test_a_wholly_sound_signature_is_untouched():
    def handler(a: int = 1, b: str = "x") -> None:
        return None

    plan = build_plan(handler)
    assert [slot.target_type for slot in plan.slots] == [int, str]


# ── it is no longer silent ───────────────────────────────────────────


def test_an_unresolvable_annotation_is_warned_about():
    """Silence is what made this dangerous."""
    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        def handler(bad: StillMissing = 2) -> None:  # noqa: F821
            return None

        build_plan(handler)


def test_the_warning_names_the_parameter_and_the_handler():
    with pytest.warns(UserWarning) as caught:

        def a_named_handler(bad: StillMissing = 2) -> None:  # noqa: F821
            return None

        build_plan(a_named_handler)

    message = str(caught[0].message)
    assert "'bad'" in message
    assert "a_named_handler" in message
    assert "TYPE_CHECKING" in message, "the warning should say how to fix it"


def test_a_parameter_bound_by_name_is_not_warned_about():
    """`request` binds from the name, so losing its annotation costs nothing.

    Veloce's own security schemes annotate `request: Request` under
    TYPE_CHECKING, so warning here would fire on correct framework code.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        def handler(request: Request) -> None:  # noqa: F821
            return None

        plan = build_plan(handler)

    assert plan.slots[0].kind == K_REQUEST
    assert not [w for w in caught if "could not resolve" in str(w.message)]


def test_a_sound_signature_warns_about_nothing():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        def handler(a: int = 1) -> None:
            return None

        build_plan(handler)

    assert not [w for w in caught if "could not resolve" in str(w.message)]


def _denies(request):
    """A guard that refuses everything, so a bypass shows up as a 200."""
    raise RuntimeError("this dependency must never run")


def test_a_pep604_union_with_a_default_only_warns():
    """POSITIVE: `Tag | None` must not be stricter than `Optional[Tag]`.

    The placeholder defined no `__or__`, so evaluating the union raised and the
    "cannot tell" branch refused - while the identical `Optional[Tag]` spelling
    evaluated fine and only warned. The docs promise a parameter that has a
    default still only warns.
    """
    app = Veloce()

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.get("/pep604")
        async def pep604(tag: Missing | None = None):  # noqa: F821
            return {"tag": tag}

    with TestClient(app) as client:
        assert client.get("/pep604").json() == {"tag": None}


def test_a_marker_on_the_left_of_a_union_is_still_refused():
    """NEGATIVE: a union must not become a way to drop a `Security` marker."""
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/unionmarker")
        async def unionmarker(user: Ann[Missing, Security(_denies)] | None = None):  # noqa: F821
            return {"user": user}


def test_a_typing_extensions_doc_annotation_only_warns():
    """POSITIVE: `Doc()` carries no behaviour, so its loss changes nothing.

    The style guide mandates `Annotated[T, Doc(...)]` on public surfaces, and
    ruff moves `from typing_extensions import Doc` under TYPE_CHECKING. `Doc`
    then became a placeholder in a metadata slot, which the fail-closed rule
    refused - a route with no vulnerability to close.
    """
    app = Veloce()

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.get("/documented")
        async def documented(q: Annotated[Missing, Doc("a query")] = "x"):  # noqa: F821
            return {"q": q}

    with TestClient(app) as client:
        assert client.get("/documented").json() == {"q": "x"}


def test_a_marker_beside_a_typing_name_is_still_refused():
    """NEGATIVE: resolving `Doc` must not stop the scan finding `Security`."""
    app = Veloce()

    with pytest.raises(TypeError, match="user"):

        @app.get("/docandguard")
        async def docandguard(
            user: Annotated[Missing, Doc("who"), Security(_denies)] = None,  # noqa: F821
        ):
            return {"user": user}


def test_a_path_parameter_with_an_unresolvable_annotation_still_registers():
    """POSITIVE: the value comes from the URL path, not the query string.

    A dropped annotation leaves a `K_QUERY` slot, which the resolver satisfies
    from `path_params` first - so the parameter is still read from the path
    exactly as declared. Refusing it closed no bypass.
    """
    app = Veloce()

    with pytest.warns(UserWarning, match="could not resolve the annotation"):

        @app.get("/users/{uid}")
        async def show(uid: Missing):  # noqa: F821
            return {"uid": uid}

    with TestClient(app) as client:
        assert client.get("/users/42").json() == {"uid": "42"}


def test_a_query_parameter_with_no_default_is_still_refused():
    """NEGATIVE: the exemption must cover the path template and nothing else.

    The same handler shape, with the parameter absent from the route template,
    degrades into a *required query* parameter - the source really did change,
    so it must still refuse.
    """
    app = Veloce()

    with pytest.raises(TypeError, match="item"):

        @app.get("/items")
        async def listing(item: Missing):  # noqa: F821
            return {"item": item}
