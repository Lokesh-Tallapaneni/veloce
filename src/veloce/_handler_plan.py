"""Handler plan — pre-computed parameter-resolution plan for a route handler.

A list of `_Slot` records - one per handler parameter - each tagged with the
source the request should be read from, plus the route-level dependencies and
the recursive plans for any reachable `Depends` sub-graph. Built once at route
registration, so reflection never happens on the hot path.

The implementation is split across sibling modules: `_plan_kinds` (slot-kind
constants), `_plan_annotations` (annotation helpers and warnings), `_plan_slot`
(the slot record), `_plan_core` (`HandlerPlan`), `_plan_hints` (signature
inspection) and `_plan_build` (the builders). This module keeps the original
import path and re-exports every name that used to live here.
"""

from __future__ import annotations

from veloce._plan_annotations import _annotated_field_info as _annotated_field_info
from veloce._plan_annotations import _callable_name as _callable_name
from veloce._plan_annotations import _group_field_specs as _group_field_specs
from veloce._plan_annotations import _guard_plain_mutable_default as _guard_plain_mutable_default
from veloce._plan_annotations import _marker_kind as _marker_kind
from veloce._plan_annotations import _owned as _owned
from veloce._plan_annotations import _raise_kwarg_ambiguity as _raise_kwarg_ambiguity
from veloce._plan_annotations import _unwrap_list as _unwrap_list
from veloce._plan_annotations import _unwrap_optional as _unwrap_optional
from veloce._plan_annotations import (
    _warn_field_constraint_uncarried as _warn_field_constraint_uncarried,
)
from veloce._plan_annotations import _warn_field_info_overridden as _warn_field_info_overridden
from veloce._plan_annotations import _warn_field_info_unused as _warn_field_info_unused
from veloce._plan_annotations import _warn_shared_mutable_default as _warn_shared_mutable_default
from veloce._plan_annotations import extract_annotated_marker as extract_annotated_marker
from veloce._plan_build import _adopt_field_marker as _adopt_field_marker
from veloce._plan_build import _build_annotated_slot as _build_annotated_slot
from veloce._plan_build import _build_depends_slot as _build_depends_slot
from veloce._plan_build import _build_marker_slot as _build_marker_slot
from veloce._plan_build import _dependency_entry_error as _dependency_entry_error
from veloce._plan_build import _extend_plan_chain as _extend_plan_chain
from veloce._plan_build import _subgraph_reads_scopes as _subgraph_reads_scopes
from veloce._plan_build import _variadic_param_error as _variadic_param_error
from veloce._plan_build import build_plan as build_plan
from veloce._plan_build import build_route_dep_plans as build_route_dep_plans
from veloce._plan_build import split_positional_only as split_positional_only
from veloce._plan_core import HandlerPlan as HandlerPlan
from veloce._plan_core import PlanExtensionProvider as PlanExtensionProvider
from veloce._plan_core import _plan_extension_providers as _plan_extension_providers
from veloce._plan_core import get_plan_extension as get_plan_extension
from veloce._plan_core import register_plan_extension as register_plan_extension
from veloce._plan_core import unregister_plan_extension as unregister_plan_extension
from veloce._plan_hints import _BOUND_BY_NAME as _BOUND_BY_NAME
from veloce._plan_hints import _INSPECT_CACHE as _INSPECT_CACHE
from veloce._plan_hints import _TYPING_NAMES as _TYPING_NAMES
from veloce._plan_hints import _annotation_markers as _annotation_markers
from veloce._plan_hints import _AnnotationProbe as _AnnotationProbe
from veloce._plan_hints import _declaration_is_load_bearing as _declaration_is_load_bearing
from veloce._plan_hints import _inspect_handler as _inspect_handler
from veloce._plan_hints import _PlaceholderNamespace as _PlaceholderNamespace
from veloce._plan_hints import _salvage_hints as _salvage_hints
from veloce._plan_hints import _unresolved_declaration_error as _unresolved_declaration_error
from veloce._plan_hints import _UnresolvedName as _UnresolvedName
from veloce._plan_hints import _warn_unresolved_annotations as _warn_unresolved_annotations
from veloce._plan_kinds import _NO_PATH_PARAMS as _NO_PATH_PARAMS
from veloce._plan_kinds import _SEQUENCE_ORIGINS as _SEQUENCE_ORIGINS
from veloce._plan_kinds import _SEQUENCE_TYPES as _SEQUENCE_TYPES
from veloce._plan_kinds import K_BG_TASKS as K_BG_TASKS
from veloce._plan_kinds import K_BODY_MODEL as K_BODY_MODEL
from veloce._plan_kinds import K_DEPENDS as K_DEPENDS
from veloce._plan_kinds import K_MODEL_GROUP as K_MODEL_GROUP
from veloce._plan_kinds import K_PARAM_MARKER as K_PARAM_MARKER
from veloce._plan_kinds import K_QUERY as K_QUERY
from veloce._plan_kinds import K_QUERY_LIST as K_QUERY_LIST
from veloce._plan_kinds import K_REQUEST as K_REQUEST
from veloce._plan_kinds import K_RESPONSE as K_RESPONSE
from veloce._plan_kinds import K_SECURITY_SCOPES as K_SECURITY_SCOPES
from veloce._plan_kinds import K_UPLOAD_FILE as K_UPLOAD_FILE
from veloce._plan_kinds import K_WEBSOCKET as K_WEBSOCKET
from veloce._plan_kinds import MARKER_LOC as MARKER_LOC
from veloce._plan_kinds import MK_BODY as MK_BODY
from veloce._plan_kinds import MK_COOKIE as MK_COOKIE
from veloce._plan_kinds import MK_FILE as MK_FILE
from veloce._plan_kinds import MK_FORM as MK_FORM
from veloce._plan_kinds import MK_HEADER as MK_HEADER
from veloce._plan_kinds import MK_PATH as MK_PATH
from veloce._plan_kinds import MK_QUERY as MK_QUERY
from veloce._plan_kinds import _fixed_shape_args as _fixed_shape_args
from veloce._plan_kinds import _normalise_sequence_default as _normalise_sequence_default
from veloce._plan_kinds import _rebuild_sequence as _rebuild_sequence
from veloce._plan_slot import _cached_callables as _cached_callables
from veloce._plan_slot import _Slot as _Slot
from veloce._plan_slot import _slot_parallel_safe as _slot_parallel_safe
from veloce._plan_slot import compute_dep_waves as compute_dep_waves
