"""Plan bridge — HandlerPlan to MCP tool definition (input schema + binding).

The input schema is derived from the same `HandlerPlan` the HTTP dispatch path
uses, through the OpenAPI lowering's own type conversion, so the two cannot
drift. At call time `bind_arguments` produces the handler kwargs from the JSON
``arguments`` mapping, resolving any `Depends` graph through the shared
`DependencyResolver`.
"""

from __future__ import annotations

# Imported for its side effect: it registers the "mcp" plan extension, at the
# same moment importing this module always did.
from veloce.contrib.mcp._plan_facts import (
    _compute_mcp_plan_facts as _compute_mcp_plan_facts,
)
from veloce.contrib.mcp.plan_bridge._binding import (
    _background_tasks as _background_tasks,
)
from veloce.contrib.mcp.plan_bridge._binding import (
    _build_request as _build_request,
)
from veloce.contrib.mcp.plan_bridge._binding import (
    _carrier_scope as _carrier_scope,
)
from veloce.contrib.mcp.plan_bridge._binding import (
    _injected_response as _injected_response,
)
from veloce.contrib.mcp.plan_bridge._binding import (
    bind_arguments as bind_arguments,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _JSON_TYPE_NAMES as _JSON_TYPE_NAMES,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _SCALAR_TARGETS as _SCALAR_TARGETS,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _UNION_ORIGINS as _UNION_ORIGINS,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _bind_model_group as _bind_model_group,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _build_sequence as _build_sequence,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _coerce_argument as _coerce_argument,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _coerce_json_scalar as _coerce_json_scalar,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _coerce_list_item as _coerce_list_item,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _coerce_list_members as _coerce_list_members,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _coerce_nested_sequence as _coerce_nested_sequence,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _coerce_union_member as _coerce_union_member,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _declared_type_name as _declared_type_name,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _json_type_name as _json_type_name,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _member_contract as _member_contract,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _scalar_str as _scalar_str,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _union_members as _union_members,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _validate_adapted as _validate_adapted,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _validate_marker_bound as _validate_marker_bound,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _validate_model as _validate_model,
)
from veloce.contrib.mcp.plan_bridge._coercion import (
    _wrong_type as _wrong_type,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _MCP_REF_PREFIX as _MCP_REF_PREFIX,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _OPENAPI_REF_PREFIX as _OPENAPI_REF_PREFIX,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    JSON_SCHEMA_DIALECT as JSON_SCHEMA_DIALECT,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _collect_defs as _collect_defs,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _collect_input_slots as _collect_input_slots,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _deepcopy_schema as _deepcopy_schema,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _item_schema as _item_schema,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _rewrite_refs as _rewrite_refs,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _slot_schema as _slot_schema,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _spread_model_fields as _spread_model_fields,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    _with_null_branch as _with_null_branch,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    build_input_schema as build_input_schema,
)
from veloce.contrib.mcp.plan_bridge._schema import (
    build_output_schema as build_output_schema,
)
from veloce.contrib.mcp.plan_bridge._slots import (
    _ANNOTATION_KEYWORDS as _ANNOTATION_KEYWORDS,
)
from veloce.contrib.mcp.plan_bridge._slots import (
    _INPUT_KINDS as _INPUT_KINDS,
)
from veloce.contrib.mcp.plan_bridge._slots import (
    _is_context_slot as _is_context_slot,
)
from veloce.contrib.mcp.plan_bridge._slots import (
    _plan_reaches_context as _plan_reaches_context,
)

__all__ = [
    "JSON_SCHEMA_DIALECT",
    "_ANNOTATION_KEYWORDS",
    "_INPUT_KINDS",
    "_JSON_TYPE_NAMES",
    "_MCP_REF_PREFIX",
    "_OPENAPI_REF_PREFIX",
    "_SCALAR_TARGETS",
    "_UNION_ORIGINS",
    "_background_tasks",
    "_bind_model_group",
    "_build_request",
    "_build_sequence",
    "_carrier_scope",
    "_coerce_argument",
    "_coerce_json_scalar",
    "_coerce_list_item",
    "_coerce_list_members",
    "_coerce_nested_sequence",
    "_coerce_union_member",
    "_collect_defs",
    "_collect_input_slots",
    "_declared_type_name",
    "_deepcopy_schema",
    "_injected_response",
    "_is_context_slot",
    "_item_schema",
    "_json_type_name",
    "_member_contract",
    "_plan_facts",
    "_plan_reaches_context",
    "_rewrite_refs",
    "_scalar_str",
    "_slot_schema",
    "_spread_model_fields",
    "_union_members",
    "_validate_adapted",
    "_validate_marker_bound",
    "_validate_model",
    "_with_null_branch",
    "_wrong_type",
    "bind_arguments",
    "build_input_schema",
    "build_output_schema",
]
