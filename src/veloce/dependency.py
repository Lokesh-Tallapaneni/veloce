"""Dependency injection — pre-planned at registration, executed per request.

Public API: `Depends`, `Security`, `SecurityScopes`. The resolver walks a
pre-built `HandlerPlan` (see `veloce._handler_plan`) rather than reflecting on
the handler signature per request.

The implementation is split by responsibility across sibling modules:
`_dependency_markers` holds the declarations users write,
`_dependency_coerce` the string-to-type coercion and validation helpers, and
`_dependency_resolver` the per-request walk. This module keeps the original
import path and re-exports every name that used to live here.
"""

from __future__ import annotations

from veloce._dependency_coerce import (
    _as_declared_sequence as _as_declared_sequence,
)
from veloce._dependency_coerce import (
    _coerce_scalar as _coerce_scalar,
)
from veloce._dependency_coerce import (
    _coerce_value as _coerce_value,
)
from veloce._dependency_coerce import (
    _collect_sequence_marker as _collect_sequence_marker,
)
from veloce._dependency_coerce import (
    _err_body_validation as _err_body_validation,
)
from veloce._dependency_coerce import (
    _err_missing_marker as _err_missing_marker,
)
from veloce._dependency_coerce import (
    _is_non_integer_float as _is_non_integer_float,
)
from veloce._dependency_coerce import (
    _list_marker_checks as _list_marker_checks,
)
from veloce._dependency_coerce import (
    _msgspec_validation_error as _msgspec_validation_error,
)
from veloce._dependency_coerce import (
    _validate_body_list as _validate_body_list,
)
from veloce._dependency_coerce import (
    _validate_list_marker as _validate_list_marker,
)
from veloce._dependency_markers import Depends as Depends
from veloce._dependency_markers import Security as Security
from veloce._dependency_markers import SecurityScopes as SecurityScopes
from veloce._dependency_resolver import _NOT_COMPILABLE as _NOT_COMPILABLE
from veloce._dependency_resolver import DependencyResolver as DependencyResolver
from veloce._handler_plan import _fixed_shape_args as _fixed_shape_args
from veloce.exceptions import RequestValidationError as RequestValidationError
