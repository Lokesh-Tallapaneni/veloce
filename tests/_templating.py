"""Installing a prebuilt `Jinja2Templates` on an already-constructed app.

`Veloce(template_folder=...)` is the only supported way to bind the templating
slot, so a test that needs a `Jinja2Templates` it built itself - with a
`ChoiceLoader`, a custom `Environment`, or just a `tmp_path` directory decided
after the app exists - has to write `app._templates` directly. Ten sites across
three modules did, each pinning the private name independently.

The write is here instead, once. `app.jinja_env` is public and covers adding
filters and globals or swapping the loader; what has no public seam is
installing a *different* `Jinja2Templates` object, and that is what this does.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates


def install_templates(app: Veloce, templates: Jinja2Templates) -> Jinja2Templates:
    """Bind `templates` as `app`'s template environment; return it."""
    app._templates = templates
    return templates


def templates_at(app: Veloce, directory: str) -> Jinja2Templates:
    """Build a `Jinja2Templates` over `directory` and install it on `app`."""
    return install_templates(app, Jinja2Templates(directory=directory))
