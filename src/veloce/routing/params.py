"""Parameter markers — re-exported from `veloce._params`.

The classes themselves live at `veloce._params`, outside this package: reaching
them from inside `routing` initialises the package, which imports `router`, which
needs `_handler_plan`, which needs these markers. See that module for the whole
story. This gateway keeps `veloce.routing.params` working as the public path.

Redundant aliases mark the names as deliberate re-exports; `__all__` belongs to
`__init__.py` gateways, and this is a leaf module.
"""

from __future__ import annotations

from veloce._params import Body as Body
from veloce._params import Cookie as Cookie
from veloce._params import File as File
from veloce._params import Form as Form
from veloce._params import Header as Header
from veloce._params import ParamBase as ParamBase
from veloce._params import Path as Path
from veloce._params import Query as Query
