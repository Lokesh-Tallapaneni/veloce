"""OpenAPI 3.1 schema generation — auto-generated from routes."""

from __future__ import annotations

# The schema layer lives in `_jsonschema`; these names have always resolved here.
from veloce.contrib._jsonschema import (
    SchemaRegistry as SchemaRegistry,
)
from veloce.contrib._jsonschema import (
    _iter_dicts as _iter_dicts,
)
from veloce.contrib._jsonschema import (
    _local_def_refs as _local_def_refs,
)
from veloce.contrib._jsonschema import (
    _python_type_to_schema as _python_type_to_schema,
)
from veloce.contrib._jsonschema import (
    _rewrite_byte_format as _rewrite_byte_format,
)
from veloce.contrib._jsonschema import (
    _unique_component_name as _unique_component_name,
)
from veloce.contrib.openapi._document import (
    _validate_document as _validate_document,
)
from veloce.contrib.openapi._document import (
    get_openapi_schema as get_openapi_schema,
)
from veloce.contrib.openapi._introspection import (
    _HANDLER_INTRO_CACHE as _HANDLER_INTRO_CACHE,
)
from veloce.contrib.openapi._introspection import (
    _build_info_object as _build_info_object,
)
from veloce.contrib.openapi._introspection import (
    _deep_merge as _deep_merge,
)
from veloce.contrib.openapi._introspection import (
    _group_field_schema as _group_field_schema,
)
from veloce.contrib.openapi._introspection import (
    _grouped_model_properties as _grouped_model_properties,
)
from veloce.contrib.openapi._introspection import (
    _handler_intro as _handler_intro,
)
from veloce.contrib.openapi._introspection import (
    _param_marker as _param_marker,
)
from veloce.contrib.openapi._operations import (
    _apply_openapi_extra as _apply_openapi_extra,
)
from veloce.contrib.openapi._operations import (
    _build_operation as _build_operation,
)
from veloce.contrib.openapi._operations import (
    _disambiguate_operation_ids as _disambiguate_operation_ids,
)
from veloce.contrib.openapi._operations import (
    _operation_base as _operation_base,
)
from veloce.contrib.openapi._operations import (
    _walk_webhooks as _walk_webhooks,
)
from veloce.contrib.openapi._operations import (
    _webhook_request_body as _webhook_request_body,
)
from veloce.contrib.openapi._parameters import (
    _BodyField as _BodyField,
)
from veloce.contrib.openapi._parameters import (
    _declare_undocumented_path_params as _declare_undocumented_path_params,
)
from veloce.contrib.openapi._parameters import (
    _extract_parameters as _extract_parameters,
)
from veloce.contrib.openapi._parameters import (
    _FormField as _FormField,
)
from veloce.contrib.openapi._parameters import (
    _is_required as _is_required,
)
from veloce.contrib.openapi._parameters import (
    _RouteParameters as _RouteParameters,
)
from veloce.contrib.openapi._parameters import (
    _ScalarBody as _ScalarBody,
)
from veloce.contrib.openapi._request_body import (
    _extract_request_body as _extract_request_body,
)
from veloce.contrib.openapi._responses import (
    _AUTO_VALIDATION_ERROR_REF as _AUTO_VALIDATION_ERROR_REF,
)
from veloce.contrib.openapi._responses import (
    _HTTP_VALIDATION_ERROR_SCHEMA_NAME as _HTTP_VALIDATION_ERROR_SCHEMA_NAME,
)
from veloce.contrib.openapi._responses import (
    _VALIDATION_ERROR_COMPONENT_SCHEMAS as _VALIDATION_ERROR_COMPONENT_SCHEMAS,
)
from veloce.contrib.openapi._responses import (
    _VALIDATION_ERROR_SCHEMA_NAME as _VALIDATION_ERROR_SCHEMA_NAME,
)
from veloce.contrib.openapi._responses import (
    _VALIDATION_ERROR_STATUS as _VALIDATION_ERROR_STATUS,
)
from veloce.contrib.openapi._responses import (
    _extract_responses as _extract_responses,
)
from veloce.contrib.openapi._responses import (
    _has_validatable_params as _has_validatable_params,
)
from veloce.contrib.openapi._responses import (
    _references_validation_error_schema as _references_validation_error_schema,
)
from veloce.contrib.openapi._responses import (
    _repoint_validation_error_refs as _repoint_validation_error_refs,
)
from veloce.contrib.openapi._security import (
    _collect_security_requirements as _collect_security_requirements,
)
from veloce.contrib.openapi._security import (
    _dependency_graph_has_validatable as _dependency_graph_has_validatable,
)
from veloce.contrib.openapi._security import (
    _dependency_params as _dependency_params,
)
from veloce.contrib.openapi._security import (
    _scheme_definition as _scheme_definition,
)

__all__ = [
    "SchemaRegistry",
    "_AUTO_VALIDATION_ERROR_REF",
    "_BodyField",
    "_FormField",
    "_HANDLER_INTRO_CACHE",
    "_HTTP_VALIDATION_ERROR_SCHEMA_NAME",
    "_RouteParameters",
    "_ScalarBody",
    "_VALIDATION_ERROR_COMPONENT_SCHEMAS",
    "_VALIDATION_ERROR_SCHEMA_NAME",
    "_VALIDATION_ERROR_STATUS",
    "_apply_openapi_extra",
    "_build_info_object",
    "_build_operation",
    "_collect_security_requirements",
    "_declare_undocumented_path_params",
    "_deep_merge",
    "_dependency_graph_has_validatable",
    "_dependency_params",
    "_disambiguate_operation_ids",
    "_extract_parameters",
    "_extract_request_body",
    "_extract_responses",
    "_group_field_schema",
    "_grouped_model_properties",
    "_handler_intro",
    "_has_validatable_params",
    "_is_required",
    "_iter_dicts",
    "_local_def_refs",
    "_operation_base",
    "_param_marker",
    "_python_type_to_schema",
    "_references_validation_error_schema",
    "_repoint_validation_error_refs",
    "_rewrite_byte_format",
    "_scheme_definition",
    "_unique_component_name",
    "_validate_document",
    "_walk_webhooks",
    "_webhook_request_body",
    "get_openapi_schema",
]
