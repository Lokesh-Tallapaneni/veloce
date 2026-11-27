"""`VeloceWorker` honours gunicorn's `--ssl-version` as a TLS floor.

`build_ssl_context` read every other key out of `cfg.ssl_options` - certfile,
keyfile, ca_certs, cert_reqs, ciphers - and dropped `ssl_version` on the floor.
A deployment pinning a minimum TLS version therefore got the interpreter default
instead: not insecure, since that default is already TLS 1.2, but configuration
that looked honoured and was not.

It is mapped onto `minimum_version` rather than used to select a protocol
method, because the `PROTOCOL_TLSv1*` constants are deprecated. A floor *below*
the default is refused rather than applied - RFC 8996 deprecates TLS 1.0 and
1.1, and silently weakening the handshake would be worse than ignoring it was.

The floor logic is exercised directly against a context: loading a certificate
chain would drag in a key-generation dependency and say nothing extra about the
behaviour under test.
"""

from __future__ import annotations

import ssl

import pytest

from veloce.workers import _apply_ssl_version, _warn_if_chain_expired, build_ssl_context


def _context() -> ssl.SSLContext:
    return ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)


def _default_floor() -> ssl.TLSVersion:
    return _context().minimum_version


# ── The floor is applied ─────────────────────────────────────────────


def test_a_higher_floor_is_applied():
    """The defect: this setting used to be read and discarded."""
    context = _context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    _apply_ssl_version(context, ssl.PROTOCOL_TLSv1_2)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_none_leaves_the_default_untouched():
    context = _context()
    _apply_ssl_version(context, None)
    assert context.minimum_version == _default_floor()


def test_a_negotiate_best_setting_is_a_no_op():
    """`PROTOCOL_TLS_SERVER` means what the default context already does."""
    context = _context()
    _apply_ssl_version(context, ssl.PROTOCOL_TLS_SERVER)
    assert context.minimum_version == _default_floor()


def test_an_unknown_value_is_ignored_rather_than_raising():
    """A gunicorn build carrying a constant we do not map must still start."""
    context = _context()
    _apply_ssl_version(context, 999_999)
    assert context.minimum_version == _default_floor()


# ── A weaker floor is refused, loudly ────────────────────────────────


def test_a_lower_floor_is_refused_not_applied(caplog):
    """Silently weakening the handshake would be worse than ignoring it."""
    version = getattr(ssl, "PROTOCOL_TLSv1", None)
    if version is None:
        pytest.skip("this interpreter has no TLS 1.0 protocol constant")
    context = _context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with caplog.at_level("WARNING"):
        _apply_ssl_version(context, version)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert "ssl-version" in caplog.text


def test_the_refusal_names_both_versions(caplog):
    """A silent refusal would be as mysterious as the silent drop it replaced."""
    version = getattr(ssl, "PROTOCOL_TLSv1_1", None)
    if version is None:
        pytest.skip("this interpreter has no TLS 1.1 protocol constant")
    context = _context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with caplog.at_level("WARNING"):
        _apply_ssl_version(context, version)
    assert "TLSv1_1" in caplog.text
    assert "TLSv1_2" in caplog.text


def test_an_equal_floor_is_applied_without_warning(caplog):
    """Equal is not weaker; it must not be refused."""
    context = _context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with caplog.at_level("WARNING"):
        _apply_ssl_version(context, ssl.PROTOCOL_TLSv1_2)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert caplog.text == ""


# ── The surrounding contract is unchanged ────────────────────────────


def test_a_missing_certfile_still_fails_fast():
    with pytest.raises(RuntimeError, match="certfile"):
        build_ssl_context({"keyfile": "/nonexistent/key.pem"})


def test_an_unloadable_chain_still_fails_fast():
    with pytest.raises(RuntimeError, match="cert chain"):
        build_ssl_context({"certfile": "/nonexistent/cert.pem"})


# ── Certificate validity is reported, not enforced ───────────────────
#
# The dates are driven through a stubbed decoder rather than a real expired
# certificate: generating one needs a key-generation dependency, and what is
# worth pinning here is the date arithmetic and the wording, not OpenSSL's
# parser.


def _decode_as(monkeypatch, **fields):
    """Make the certificate decoder return exactly `fields`."""
    monkeypatch.setattr(ssl._ssl, "_test_decode_cert", lambda _path: dict(fields), raising=False)


def test_an_expired_certificate_is_warned_about(monkeypatch, caplog):
    """`load_cert_chain` accepts it, so nothing else says why handshakes fail."""
    _decode_as(monkeypatch, notAfter="Jan  1 00:00:00 2000 GMT")
    with caplog.at_level("WARNING"):
        _warn_if_chain_expired("/any/cert.pem")
    assert "expires" in caplog.text
    assert "2000" in caplog.text


def test_a_not_yet_valid_certificate_is_warned_about(monkeypatch, caplog):
    _decode_as(monkeypatch, notBefore="Jan  1 00:00:00 2999 GMT")
    with caplog.at_level("WARNING"):
        _warn_if_chain_expired("/any/cert.pem")
    assert "is not valid until" in caplog.text


def test_a_current_certificate_is_silent(monkeypatch, caplog):
    """A healthy deployment must not log a warning on every worker start."""
    _decode_as(
        monkeypatch,
        notBefore="Jan  1 00:00:00 2000 GMT",
        notAfter="Jan  1 00:00:00 2999 GMT",
    )
    with caplog.at_level("WARNING"):
        _warn_if_chain_expired("/any/cert.pem")
    assert caplog.text == ""


def test_an_unparseable_date_is_passed_over(monkeypatch, caplog):
    _decode_as(monkeypatch, notAfter="whenever")
    with caplog.at_level("WARNING"):
        _warn_if_chain_expired("/any/cert.pem")
    assert caplog.text == ""


def test_an_unreadable_file_produces_no_bogus_warning(tmp_path, caplog):
    """OpenSSL already accepted the chain; do not invent an expiry claim."""
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate", encoding="utf-8")
    with caplog.at_level("WARNING"):
        _warn_if_chain_expired(str(junk))
    assert caplog.text == ""


def test_a_missing_file_is_passed_over(caplog):
    """Start-up already failed for a missing chain; do not double-report it."""
    with caplog.at_level("WARNING"):
        _warn_if_chain_expired("/nonexistent/cert.pem")
    assert caplog.text == ""
