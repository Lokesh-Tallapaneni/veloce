"""A test name defined in two modules is bounded, and never copy-paste.

A name that appears twice is not automatically wrong: `test_query_params` on the
sync client, the async client and the raw request object is the same property on
three genuinely different surfaces, and pytest reports `module.py::name` anyway.
What *is* wrong is a body copied verbatim into a second module, where the copy
drifts silently or covers nothing the original did not.

Measured, rather than assumed: of the 50 names shared across modules, **two**
had byte-identical bodies. Both are fixed - `test_safe_join_rejects_nul_byte`
was a unit assertion duplicated into a module documented as end-to-end, and
three guard modules each carried the same two-line `test_the_scan_covers_the_suite`.

So this module enforces the distinction: no byte-identical duplicate at all, and
the coincidental ones frozen so a new name cannot join them unnoticed.
"""

from __future__ import annotations

import ast
import collections
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent

#: Names defined in more than one module today, each a different surface.
#: Adding to this list should mean deciding the new pair is genuinely two
#: surfaces rather than one test copied.
KNOWN_SHARED = {
    "test_a_body_exactly_at_the_limit_is_served": {
        "test_body_limit_early_reject.py",
        "test_body_limit_refusal_points.py",
    },
    "test_a_declared_over_limit_body_is_refused": {
        "test_body_limit_refusal_points.py",
        "test_max_content_length.py",
    },
    "test_a_sync_hook_is_supported": {
        "test_mcp_call_hooks.py",
        "test_process_response_matches_dispatch.py",
    },
    "test_a_value_that_is_not_a_boolean_is_refused": {
        "test_bool_query_coercion.py",
        "test_env_file_value_validation.py",
    },
    "test_add_event_handler_emits_deprecation_warning": {
        "test_add_event_handler.py",
        "test_deprecation_warnings.py",
    },
    "test_an_explicit_content_type_still_wins": {
        "test_content_type_guessing.py",
        "test_make_response_agreement.py",
    },
    "test_an_unsupported_grant_type_is_refused": {
        "test_mcp_authorization_server.py",
        "test_mcp_oauth_grant_types.py",
    },
    "test_async_hook_supported": {"test_before_first_request.py", "test_instrumentation.py"},
    "test_bearer_extracts_token": {"test_datastructures.py", "test_request_auth.py"},
    "test_both_transports_agree_on_content_type": {
        "test_native_refusal_response_phase.py",
        "test_native_transport_parity.py",
    },
    "test_both_transports_agree_on_status": {
        "test_native_refusal_response_phase.py",
        "test_native_transport_parity.py",
    },
    "test_both_transports_agree_on_the_body": {
        "test_native_refusal_response_phase.py",
        "test_native_transport_parity.py",
    },
    "test_charset_default_utf8": {"test_request_aliases.py", "test_response_charset_setter.py"},
    "test_cors_preflight": {"test_app.py", "test_e2e_smoke.py"},
    "test_drain_returns_immediately_when_writable": {
        "test_server_write_backpressure.py",
        "test_websocket_native_backpressure.py",
    },
    "test_dump_cookie_basic": {"test_cookie_helpers.py", "test_cookies.py"},
    "test_dump_cookie_rejects_crlf_in_samesite": {"test_cookies.py", "test_http_e2e.py"},
    "test_empty_body": {"test_asgi_body_source.py", "test_request_get_data.py"},
    "test_empty_header_returns_none": {"test_datastructures.py", "test_request_auth.py"},
    "test_every_python_block_parses": {
        "test_database_and_graphql_guides.py",
        "test_mcp_guide_claims.py",
    },
    "test_explicit_context_wins_over_processor": {
        "test_context_processor.py",
        "test_update_template_context.py",
    },
    "test_from_bytes_does_not_re_encode": {
        "test_json_dialect_reaches_every_surface.py",
        "test_json_response_classes.py",
    },
    "test_head_falls_back_to_get": {"test_hybrid_router.py", "test_router.py"},
    "test_headers_case_insensitive": {"test_datastructures.py", "test_multidict_semantics.py"},
    "test_if_none_match_supersedes_if_modified_since": {
        "test_response_conditional.py",
        "test_static_conditional.py",
        "test_staticfiles_conditional_parity.py",
    },
    "test_import_error_sentinel_shape": {"test_metrics.py", "test_otel.py"},
    "test_importable_from_package_root": {
        "test_websocket_exception.py",
        "test_websocket_request_validation.py",
    },
    "test_include_keeps_only_the_named_fields": {
        "test_response_model_dump_kwargs.py",
        "test_response_model_filtering.py",
    },
    "test_include_with_extra_prefix": {"test_hybrid_router.py", "test_router.py"},
    "test_inline_disposition": {
        "test_content_disposition.py",
        "test_fileresponse_disposition_type.py",
    },
    "test_install_hint_names_the_optional_extra": {
        "test_metrics.py",
        "test_otel.py",
        "test_workers.py",
    },
    "test_method_not_allowed": {"test_app.py", "test_router.py"},
    "test_mimetype_lowercased": {"test_request_mimetype.py", "test_response_mimetype.py"},
    "test_multiple_params": {"test_response_mimetype_params.py", "test_router.py"},
    "test_nested": {"test_encoder_registry.py", "test_jsonable_encoder.py"},
    "test_no_limit_configured_serves_a_large_body": {
        "test_body_limit_early_reject.py",
        "test_body_limit_refusal_points.py",
    },
    "test_post_form_data": {"test_async_test_client.py", "test_testclient_request.py"},
    "test_query_params": {"test_app.py", "test_async_test_client.py", "test_testclient_request.py"},
    "test_realm_with_control_chars_raises_at_construction": {
        "test_http_basic.py",
        "test_http_digest.py",
    },
    "test_redirect_not_followed_by_default": {
        "test_async_test_client.py",
        "test_testclient_redirects.py",
    },
    "test_repeated_requests_are_stable": {
        "test_compression_negotiation.py",
        "test_datetime_converter_accelerator.py",
        "test_resolver_inlined_coercion.py",
    },
    "test_stream_empty_body_yields_nothing": {
        "test_request_data_stream.py",
        "test_request_streaming.py",
    },
    "test_subclass_without_slots_is_rejected": {"test_mcp_capabilities.py", "test_mcp_content.py"},
    "test_swagger_ui": {"test_e2e_smoke.py", "test_openapi_customization.py"},
    "test_teardown_appcontext_not_fired_on_shutdown": {
        "test_async_safety.py",
        "test_teardown_appcontext.py",
    },
    "test_the_message_says_what_to_do_instead": {
        "test_default_response_class_contract.py",
        "test_methodview_marker_refusal.py",
    },
    "test_the_refusal_names_the_argument_and_both_types": {
        "test_mcp_argument_contract.py",
        "test_mcp_tool_transform.py",
    },
    "test_the_refusal_shows_the_value": {
        "test_app_metadata_and_prefix_scope.py",
        "test_instance_path.py",
    },
    "test_there_is_one_payload_builder": {
        "test_docstring_claims_hold.py",
        "test_error_payload_agreement.py",
    },
}


def _definitions() -> dict[str, list[tuple[str, int, str]]]:
    found: dict[str, list[tuple[str, int, str]]] = collections.defaultdict(list)
    for path in sorted(TESTS.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                found[node.name].append((path.name, node.lineno, ast.unparse(node)))
    return found


DEFINITIONS = _definitions()


def test_the_scan_found_the_suite() -> None:
    """Both checks below pass trivially on an empty scan."""
    assert len(DEFINITIONS) > 2000


def test_no_test_body_is_copied_verbatim_into_another_module() -> None:
    """The half that is always a defect: the same body under the same name."""
    copies: list[str] = []
    for name, entries in DEFINITIONS.items():
        by_body: dict[str, list[str]] = collections.defaultdict(list)
        for module, line, body in entries:
            by_body[body].append(f"{module}:{line}")
        for where in by_body.values():
            if len(where) > 1:
                copies.append(f"{name} @ {', '.join(where)}")
    assert copies == [], (
        "these test bodies are byte-identical across modules - move the "
        f"assertion to whichever module owns it: {copies}"
    )


def test_the_shared_names_are_the_ones_already_decided() -> None:
    """A new duplicate is a decision, not something that happens quietly.

    The module *set* is frozen, not just the name: freezing names alone cannot
    see a third module joining an existing pair, which is the commonest way one
    of these grows.
    """
    shared = {
        name: {module for module, _, _ in entries}
        for name, entries in DEFINITIONS.items()
        if len({module for module, _, _ in entries}) > 1
    }
    added = sorted(set(shared) - set(KNOWN_SHARED))
    removed = sorted(set(KNOWN_SHARED) - set(shared))
    grew = sorted(
        f"{name}: {sorted(shared[name] - KNOWN_SHARED[name])}"
        for name in set(shared) & set(KNOWN_SHARED)
        if shared[name] != KNOWN_SHARED[name]
    )
    assert not added, (
        f"these names are now defined in more than one module: {added}. If the "
        "two really are different surfaces, add them to KNOWN_SHARED; if one is "
        "a copy, move it instead."
    )
    assert not grew, f"these shared names gained a module: {grew}"
    assert not removed, f"KNOWN_SHARED lists names that are no longer shared: {removed}"
