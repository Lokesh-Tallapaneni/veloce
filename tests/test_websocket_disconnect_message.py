"""`WebSocketDisconnect` stringifies the same way however it was constructed.

`__init__` set `self.code` and never called `super().__init__`, and
`BaseException.__new__` populates `args` from **positional** arguments only. So
the exception's message depended on the call form:

    WebSocketDisconnect(1006)        str() == "1006"
    WebSocketDisconnect(code=1006)   str() == ""
    WebSocketDisconnect()            str() == ""

Same close code, different log line, decided by whether the caller happened to
use a keyword. A handler doing `logger.info("closed: %s", exc)` got the code
from some call sites and nothing from others.
"""

from __future__ import annotations

import pytest

from veloce.exceptions import WebSocketDisconnect
from veloce.status import WS_1000_NORMAL_CLOSURE


@pytest.mark.parametrize("code", [1000, 1001, 1006, 4000])
def test_positional_and_keyword_construction_agree(code):
    """The defect, stated directly."""
    assert str(WebSocketDisconnect(code)) == str(WebSocketDisconnect(code=code))


@pytest.mark.parametrize("code", [1000, 1001, 1006, 4000])
def test_the_message_carries_the_code(code):
    assert str(WebSocketDisconnect(code=code)) == str(code)


@pytest.mark.parametrize("code", [1000, 1006])
def test_args_carry_the_code(code):
    """`args` is what `repr`, pickling and `except ... as e: e.args` read."""
    assert WebSocketDisconnect(code=code).args == (code,)


def test_the_default_is_a_normal_closure_and_says_so():
    exc = WebSocketDisconnect()
    assert exc.code == WS_1000_NORMAL_CLOSURE
    assert str(exc) == str(WS_1000_NORMAL_CLOSURE)


def test_the_code_attribute_is_still_the_documented_accessor():
    """The negative: adding the message must not disturb `.code`."""
    assert WebSocketDisconnect(4001).code == 4001
    assert WebSocketDisconnect(code=4001).code == 4001


def test_it_is_still_catchable_as_its_own_type():
    with pytest.raises(WebSocketDisconnect) as raised:
        raise WebSocketDisconnect(code=1006)
    assert raised.value.code == 1006
