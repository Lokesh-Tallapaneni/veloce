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

from veloce import Depends, Veloce
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
