"""Tests for the Secret wrapper."""

from __future__ import annotations

import orjson
import pytest

from veloce import Secret
from veloce.encoders import jsonable_encoder, orjson_default
from veloce.passwords import hash_password, verify_password
from veloce.signing import Signer


def test_renders_masked_everywhere():
    s = Secret("topsecret")
    assert "topsecret" not in repr(s)
    assert "topsecret" not in str(s)
    assert "topsecret" not in f"{s}"
    assert "topsecret" not in "{}".format(s)  # noqa: UP032
    assert "topsecret" not in ("%s" % s)  # noqa: UP031
    assert str(s) == "***"


def test_reveal_preserves_type():
    assert Secret("x").reveal() == "x"
    assert isinstance(Secret("x").reveal(), str)
    assert Secret(b"x").reveal() == b"x"
    assert isinstance(Secret(b"x").reveal(), bytes)


def test_non_str_bytes_raises():
    with pytest.raises(TypeError):
        Secret(123)  # type: ignore[arg-type]


def test_signer_accepts_secret():
    token = Signer(Secret("k")).dumps({"a": 1})
    assert Signer("k").loads(token) == {"a": 1}
    signer = Signer("primary")
    signer.add_fallback_secret(Secret("old"))


def test_passwords_accept_secret():
    stored = hash_password(Secret("pw"))
    assert verify_password(stored, "pw") is True
    assert verify_password(stored, Secret("pw")) is True


def test_json_refusal():
    with pytest.raises(TypeError):
        orjson.dumps({"k": Secret("x")}, default=orjson_default)
    with pytest.raises(TypeError):
        jsonable_encoder(Secret("x"))
    with pytest.raises(TypeError):
        jsonable_encoder({"k": Secret("x")})
    with pytest.raises(TypeError):
        jsonable_encoder([Secret("x")])


def test_equality_constant_time():
    assert Secret("x") == Secret("x")
    assert Secret("x") != Secret("y")
    assert (Secret("x") == "x") is False


def test_unhashable():
    with pytest.raises(TypeError):
        hash(Secret("x"))


def test_bool():
    assert bool(Secret("x")) is True
    assert bool(Secret("")) is False
