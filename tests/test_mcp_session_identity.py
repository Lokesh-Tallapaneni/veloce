"""`ctx.session_id` is unique across processes, so it is safe to key state on.

`MCPSession.connection_id` comes from an `itertools.count` that restarts at 1 in
every process. That is fine for what it was built for - task ownership and the
in-flight registry, both of which are per-process - but the same value was also
handed to application code as `MCPContext.session_id`. Under `--workers 4` four
unrelated clients on four workers were all told they were session 1, so a
handler keying per-client state on it silently shared one bucket between them.

The internal key stays the cheap int; the public identity carries a per-process
token as well.
"""

from __future__ import annotations

import re

from veloce.contrib.mcp.session import MCPSession


def test_the_public_id_is_a_string():
    """`session_id` is annotated `str | None`; it used to return an int."""
    assert isinstance(MCPSession().public_id, str)


def test_two_sessions_in_one_process_differ():
    assert MCPSession().public_id != MCPSession().public_id


def test_the_public_id_carries_the_connection_id():
    session = MCPSession()
    assert session.public_id.endswith(f"-{session.connection_id}")


def test_the_internal_key_stays_an_int():
    """Task ownership and the in-flight registry key on this; keep it cheap."""
    assert isinstance(MCPSession().connection_id, int)


def test_the_process_token_is_not_guessable_sequential():
    """A counter-derived prefix would collide across workers just as badly."""
    token = MCPSession().public_id.rsplit("-", 1)[0]
    assert re.fullmatch(r"[0-9a-f]{8}", token), token


def test_sessions_in_different_processes_do_not_collide():
    """The actual defect, reproduced the way it happens: a forked worker.

    Each process re-imports the module, so the counter restarts - and before the
    fix both processes reported the same id for their first connection.
    """
    import contextlib
    import multiprocessing as mp

    if mp.get_start_method(allow_none=True) is None:
        with contextlib.suppress(RuntimeError):
            mp.set_start_method("spawn")

    with mp.Pool(2) as pool:
        ids = pool.map(_first_public_id, [None, None])
    first_ints = pool_int_suffixes(ids)
    # Both processes minted connection id 1 - that is the collision the public
    # id has to survive.
    assert first_ints == [1, 1], first_ints
    assert ids[0] != ids[1], ids


def pool_int_suffixes(ids: list[str]) -> list[int]:
    return [int(value.rsplit("-", 1)[1]) for value in ids]


def _first_public_id(_: object) -> str:
    """Return the first session's public id in a fresh interpreter."""
    from veloce.contrib.mcp.session import MCPSession as Fresh

    return Fresh().public_id
