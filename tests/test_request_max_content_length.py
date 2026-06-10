"""Request.max_content_length body-size cap accessor."""

from __future__ import annotations

from veloce import Request, Veloce


def test_none_when_no_app_bound():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.max_content_length is None


def test_default_when_config_unset():
    app = Veloce()
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", app=app)
    # MAX_CONTENT_LENGTH defaults to 100 MiB in the seeded config.
    assert req.max_content_length == 100 * 1024 * 1024


def test_reads_configured_limit():
    app = Veloce()
    app.config["MAX_CONTENT_LENGTH"] = 1024
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", app=app)
    assert req.max_content_length == 1024


def test_reflects_config_changes():
    app = Veloce()
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", app=app)
    assert req.max_content_length == 100 * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = 4096
    assert req.max_content_length == 4096
