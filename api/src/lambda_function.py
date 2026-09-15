import re
import traceback

from src.lib.response import json_response
from src.routes import (
    handle_admin_create_category,
    handle_admin_create_item,
    handle_admin_create_user,
    handle_admin_meta,
    handle_admin_update_item,
    handle_dashboard_summary,
    handle_get_inventory_items,
    handle_login,
    handle_update_inventory,
)
from src.routes.suppliers import handle_get_suppliers, handle_create_supplier
from src.routes.bill_mappings import handle_get_bill_mappings, handle_create_bill_mapping, handle_suggest_mappings
from src.routes.item_pricing import handle_get_item_pricing, handle_update_item_base_price
from src.routes.bills import (
    handle_parse_hyperpure,
    handle_confirm_bill,
    handle_get_bills,
    handle_get_bill_detail,
    handle_get_bill_download_url,
    handle_update_bill,
    handle_delete_bill,
    handle_get_deleted_bills,
)
from src.routes.snapshots import handle_get_snapshots
from src.routes.food_cost import handle_get_food_cost
from src.routes.update_history import handle_get_update_history, handle_get_update_detail


# Stage prefixes that API Gateway may prepend.
_STAGE_PREFIXES = ("", "/prod", "/dev", "/stage")

# Pattern to detect {param} placeholders in route definitions.
_PARAM_RE = re.compile(r"\{(\w+)\}")


def _build_routes(raw_routes: list[tuple[str, str, callable]]):
    """Build route table from (method, path_pattern, handler) tuples.

    Supports exact paths and parameterized paths like /items/{itemId}/base-price.
    Automatically registers each route under all stage prefixes.
    """
    exact = {}
    parameterized = []

    for method, pattern, handler in raw_routes:
        has_params = bool(_PARAM_RE.search(pattern))
        for prefix in _STAGE_PREFIXES:
            full = prefix + pattern
            if has_params:
                # Convert {param} to named regex groups
                regex = _PARAM_RE.sub(r"(?P<\1>[^/]+)", full)
                parameterized.append((method, re.compile(f"^{regex}$"), handler))
            else:
                exact[(method, full)] = handler

    return exact, parameterized


_RAW_ROUTES = [
    ("POST",  "/api/v1/auth/login",               handle_login),
    ("GET",   "/api/v1/inventory/items",           handle_get_inventory_items),
    ("POST",  "/api/v1/inventory/update",          handle_update_inventory),
    ("GET",   "/api/v1/dashboard/summary",         handle_dashboard_summary),
    ("GET",   "/api/v1/admin/meta",                handle_admin_meta),
    ("POST",  "/api/v1/admin/categories",          handle_admin_create_category),
    ("POST",  "/api/v1/admin/items",               handle_admin_create_item),
    ("PATCH", "/api/v1/admin/items",               handle_admin_update_item),
    ("POST",  "/api/v1/admin/users",               handle_admin_create_user),
    # Billing & Food Cost — Phase 1
    ("GET",   "/api/v1/suppliers",                  handle_get_suppliers),
    ("POST",  "/api/v1/suppliers",                  handle_create_supplier),
    ("GET",   "/api/v1/bill-mappings",              handle_get_bill_mappings),
    ("POST",  "/api/v1/bill-mappings",              handle_create_bill_mapping),
    ("GET",   "/api/v1/bill-mappings/suggest",      handle_suggest_mappings),
    ("GET",   "/api/v1/items/pricing",              handle_get_item_pricing),
    ("PATCH", "/api/v1/items/{itemId}/base-price",  handle_update_item_base_price),
    # Billing & Food Cost — Phase 2 (Bills)
    ("POST",  "/api/v1/bills/parse-hyperpure",      handle_parse_hyperpure),
    ("POST",  "/api/v1/bills/confirm",               handle_confirm_bill),
    ("GET",   "/api/v1/bills",                       handle_get_bills),
    ("GET",   "/api/v1/bills/deleted",               handle_get_deleted_bills),
    ("GET",   "/api/v1/bills/{billId}",              handle_get_bill_detail),
    ("DELETE", "/api/v1/bills/{billId}",             handle_delete_bill),
    ("PATCH", "/api/v1/bills/{billId}",              handle_update_bill),
    ("GET",   "/api/v1/bills/{billId}/download-url", handle_get_bill_download_url),
    # Billing & Food Cost — Phase 3 (Food Cost)
    ("GET",   "/api/v1/inventory-updates/snapshots", handle_get_snapshots),
    ("GET",   "/api/v1/food-cost",                   handle_get_food_cost),
    # Update History & Comparison
    ("GET",   "/api/v1/inventory-updates/history",          handle_get_update_history),
    ("GET",   "/api/v1/inventory-updates/{updateId}",       handle_get_update_detail),
]

EXACT_ROUTES, PARAM_ROUTES = _build_routes(_RAW_ROUTES)


def _extract_path(event: dict) -> str:
    # Support API Gateway REST and HTTP API payload formats.
    return event.get("path") or event.get("rawPath") or ""


def _extract_method(event: dict) -> str:
    method = event.get("httpMethod")
    if method:
        return method.upper()

    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}
    return (http_context.get("method") or "").upper()


def lambda_handler(event, context):
    method = _extract_method(event)
    path = _extract_path(event)

    print(f"REQUEST {method} {path}")

    if method == "OPTIONS":
        return json_response(200, {"ok": True})

    # Try exact match first (fast path).
    handler = EXACT_ROUTES.get((method, path))
    if handler:
        try:
            resp = handler(event, context)
            print(f"RESPONSE {method} {path} -> {resp.get('statusCode')}")
            return resp
        except Exception:
            print(f"HANDLER ERROR {method} {path}\n{traceback.format_exc()}")
            return json_response(500, {"message": "Internal server error."})

    # Try parameterized routes.
    for route_method, pattern, route_handler in PARAM_ROUTES:
        if route_method != method:
            continue
        m = pattern.match(path)
        if m:
            event["pathParameters"] = m.groupdict()
            try:
                resp = route_handler(event, context)
                print(f"RESPONSE {method} {path} -> {resp.get('statusCode')}")
                return resp
            except Exception:
                print(f"HANDLER ERROR {method} {path}\n{traceback.format_exc()}")
                return json_response(500, {"message": "Internal server error."})

    print(f"NO ROUTE {method} {path}")
    return json_response(404, {"message": f"No route for {method} {path}"})
