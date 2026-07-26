import json
from datetime import datetime, timezone

from bson import ObjectId

from src.lib.auth import extract_bearer_token, verify_token
from src.lib.mongo import get_db
from src.lib.response import json_response
from src.lib.unit_conversion import canonical_unit, convert_to_base, normalize_units


def _normalize_number(value):
    try:
        num = float(value)
        if num < 0:
            return None
        return num
    except (TypeError, ValueError):
        return None


def _parse_threshold(value):
    if value is None:
        return None
    try:
        parsed = float(value)
        if parsed < 0:
            return None
        return parsed
    except (TypeError, ValueError):
        return None


def handle_update_inventory(event, _context):
    try:
        if event.get("httpMethod") == "OPTIONS":
            return json_response(200, {"ok": True})

        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            auth = verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        body = json.loads(event.get("body") or "{}")
        branch_code = (body.get("branchCode") or body.get("branch") or "").strip().upper()
        items = body.get("items") if isinstance(body.get("items"), list) else []
        submitted_at_raw = body.get("submittedAt")

        if not branch_code or not items:
            return json_response(400, {"message": "branchCode and non-empty items are required."})

        if auth.get("branchCode") != branch_code and auth.get("role") != "admin":
            return json_response(403, {"message": "Branch access denied."})

        db = get_db()
        branch = db.branches.find_one({"code": branch_code, "isActive": True})
        if not branch:
            return json_response(404, {"message": "Branch not found."})

        prepared_item_ids = []
        prepared_rows = []
        for row in items:
            item_id = row.get("itemId") or row.get("id")
            unit = canonical_unit(row.get("unit"))
            quantity = _normalize_number(row.get("quantity"))

            if not item_id or not unit:
                return json_response(400, {"message": "Each item must include itemId/id and unit."})
            if quantity is None:
                return json_response(400, {"message": "Item quantity must be a non-negative number."})

            try:
                oid = ObjectId(item_id)
            except Exception:
                return json_response(400, {"message": f"Invalid item id: {item_id}"})

            prepared_item_ids.append(oid)
            prepared_rows.append({"itemId": oid, "unit": unit, "quantity": quantity})

        item_docs = list(db.items.find({"_id": {"$in": prepared_item_ids}, "isActive": True}))
        if len(item_docs) != len(prepared_rows):
            return json_response(400, {"message": "One or more items are invalid or inactive."})

        item_by_id = {str(doc["_id"]): doc for doc in item_docs}
        current_rows = list(
            db.inventory_current.find({"branchId": branch["_id"], "itemId": {"$in": prepared_item_ids}})
        )
        current_by_item_id = {str(row["itemId"]): row for row in current_rows}

        now = datetime.now(timezone.utc)
        batch_items = []

        for row in prepared_rows:
            item_doc = item_by_id[str(row["itemId"])]
            allowed_units = normalize_units(item_doc.get("allowedUnits", []))
            if row["unit"] not in allowed_units:
                return json_response(
                    400,
                    {"message": f"Invalid unit '{row['unit']}' for item {item_doc['name']}"},
                )

            current = current_by_item_id.get(str(row["itemId"]))
            previous_quantity = float(current["quantity"]) if current else None
            previous_unit = current.get("unit") if current else None
            previous_quantity_base = (
                float(current["quantityBase"]) if current and current.get("quantityBase") is not None else None
            )
            previous_base_unit = current.get("baseUnit") if current else None
            next_version = int(current.get("version", 0)) + 1 if current else 1

            min_threshold = _parse_threshold(item_doc.get("minThreshold"))
            item_default_unit = canonical_unit(item_doc.get("defaultUnit"))
            qty = float(row["quantity"])
            qty_base, base_unit = convert_to_base(qty, row["unit"])
            if qty_base is None or base_unit is None:
                return json_response(400, {"message": f"Unsupported unit '{row['unit']}' for item {item_doc['name']}."})

            if previous_quantity is not None and previous_quantity_base is None:
                previous_quantity_base, previous_base_unit = convert_to_base(previous_quantity, previous_unit)

            if previous_quantity is not None:
                if previous_unit != row["unit"]:
                    if qty_base is None:
                        return json_response(
                            400,
                            {"message": f"Unsupported unit conversion for new unit '{row['unit']}'."},
                        )
                    if previous_quantity_base is None:
                        return json_response(
                            400,
                            {
                                "message": (
                                    f"Unsupported unit conversion from previous unit '{previous_unit}' "
                                    f"for item {item_doc['name']}."
                                )
                            },
                        )
                    if previous_base_unit != base_unit:
                        return json_response(
                            400,
                            {
                                "message": (
                                    f"Unit group mismatch: cannot compare '{previous_unit}' and '{row['unit']}' "
                                    f"for item {item_doc['name']}."
                                )
                            },
                        )

            min_threshold_base = None
            if min_threshold is not None:
                converted_threshold, converted_threshold_base = convert_to_base(min_threshold, item_default_unit)
                if converted_threshold is not None and converted_threshold_base == base_unit:
                    min_threshold_base = converted_threshold
                else:
                    min_threshold_base = min_threshold
            threshold_for_check = min_threshold_base
            qty_for_check = qty_base
            is_below_threshold = (
                qty_base < min_threshold_base if min_threshold_base is not None else None
            )

            if previous_quantity is None:
                delta_quantity = None
                delta_quantity_base = None
                crossed_below_threshold = (
                    qty_for_check < threshold_for_check if threshold_for_check is not None else False
                )
            else:
                previous_for_compare = previous_quantity_base if previous_quantity_base is not None else previous_quantity
                delta_quantity = qty_base - previous_for_compare if previous_for_compare is not None else None
                delta_quantity_base = (
                    qty_base - previous_quantity_base
                    if qty_base is not None and previous_quantity_base is not None and base_unit == previous_base_unit
                    else None
                )
                crossed_below_threshold = (
                    previous_for_compare is not None
                    and min_threshold_base is not None
                    and previous_for_compare >= min_threshold_base
                    and qty_base < min_threshold_base
                )

            set_fields = {
                "quantity": qty_base,
                "unit": base_unit,
                "quantityBase": qty_base,
                "baseUnit": base_unit,
                "inputQuantity": qty,
                "inputUnit": row["unit"],
                "updatedBy": ObjectId(auth["userId"]),
                "updatedAt": now,
                "version": next_version,
            }
            if is_below_threshold is not None:
                set_fields["isBelowThreshold"] = is_below_threshold

            db.inventory_current.update_one(
                {"branchId": branch["_id"], "itemId": row["itemId"]},
                {
                    "$set": set_fields,
                    "$setOnInsert": {"createdAt": now},
                },
                upsert=True,
            )

            batch_items.append(
                {
                    "itemId": row["itemId"],
                    "sku": item_doc["sku"],
                    "name": item_doc["name"],
                    "categoryId": item_doc["categoryId"],
                    "previousQuantity": previous_quantity,
                    "previousUnit": previous_unit,
                    "previousQuantityBase": previous_quantity_base,
                    "newQuantity": qty_base,
                    "newUnit": base_unit,
                    "newQuantityBase": qty_base,
                    "baseUnit": base_unit,
                    "deltaQuantity": delta_quantity,
                    "deltaQuantityBase": delta_quantity_base,
                    "inputQuantity": qty,
                    "inputUnit": row["unit"],
                    "minThreshold": min_threshold_base,
                    "crossedBelowThreshold": crossed_below_threshold,
                }
            )

        submitted_at = now
        if submitted_at_raw:
            try:
                submitted_at = datetime.fromisoformat(submitted_at_raw.replace("Z", "+00:00"))
            except Exception:
                submitted_at = now

        update_doc = {
            "branchId": branch["_id"],
            "branchCode": branch["code"],
            "updatedBy": {
                "userId": ObjectId(auth["userId"]),
                "username": auth.get("username"),
            },
            "submittedAt": submitted_at,
            "createdAt": now,
            "itemCount": len(batch_items),
            "items": batch_items,
        }

        insert_result = db.inventory_updates.insert_one(update_doc)

        # Build full branch snapshot and store in separate collection
        all_current = list(db.inventory_current.find({"branchId": branch["_id"]}))
        snapshot_items = []
        for cur in all_current:
            item_doc_snap = db.items.find_one({"_id": cur["itemId"], "isActive": True})
            if not item_doc_snap:
                continue
            snapshot_items.append({
                "itemId": cur["itemId"],
                "sku": item_doc_snap.get("sku"),
                "name": item_doc_snap.get("name"),
                "categoryId": item_doc_snap.get("categoryId"),
                "quantityBase": cur.get("quantityBase"),
                "baseUnit": cur.get("baseUnit"),
                "quantity": cur.get("quantity"),
                "unit": cur.get("unit"),
            })

        db.inventory_snapshots.insert_one({
            "updateId": insert_result.inserted_id,
            "branchId": branch["_id"],
            "createdAt": now,
            "items": snapshot_items,
        })

        return json_response(
            200,
            {
                "message": "Inventory updated successfully.",
                "updateId": str(insert_result.inserted_id),
                "itemCount": len(batch_items),
                "submittedAt": submitted_at,
            },
        )
    except Exception as exc:
        print("update_inventory error", exc)
        return json_response(500, {"message": "Internal server error."})
