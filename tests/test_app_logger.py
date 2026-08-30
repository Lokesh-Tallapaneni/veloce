"""app.logger — named application logger."""

from __future__ import annotations

from veloce import Veloce


def test_logger_exists() -> None:
    # the logger name is the app's `import_name`.
    app = Veloce(import_name="my_api_pkg", openapi_url=None)
    assert app.logger is not None
    assert app.logger.name == "my_api_pkg"
