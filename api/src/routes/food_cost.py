from datetime import timedelta, timezone

from bson import ObjectId

from src.lib.auth import extract_bearer_token, verify_token
from src.lib.mongo import get_db
from src.lib.response import json_response


IST = timezone(timedelta(hours=5, minutes=30))


def _format_ist(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def handle_get_food_cost(event, _context):
    """GET /food-cost — calculate food cost between two inventory snapshots.

    Formula: Food Cost = Opening Value + Purchase Value - Closing Value

    Pricing logic:
    1. For each item, find bills between opening and closing dates with confirmed mappings.
    2. If bills found → weighted average price per base unit.
    3. If no bills → use item.basePricePerUnit (manually set base price).
    4. Same price applied to both opening and closing quantities.
    """
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        qs = event.get("queryStringParameters") or {}
        branch_code = (qs.get("branchCode") or "").strip().upper()
        opening_id = qs.get("openingUpdateId")
        closing_id = qs.get("closingUpdateId")

        if not branch_code or not opening_id or not closing_id:
            return json_response(400, {
                "message": "branchCode, openingUpdateId, and closingUpdateId are required.",
            })

        try:
            opening_oid = ObjectId(opening_id)
            closing_oid = ObjectId(closing_id)
        except Exception:
            return json_response(400, {"message": "Invalid update IDs."})

        db = get_db()

        branch = db.branches.find_one({"code": branch_code, "isActive": True})
        if not branch:
            return json_response(404, {"message": "Branch not found."})

        # Fetch opening and closing snapshots
        opening_update = db.inventory_updates.find_one({"_id": opening_oid, "branchId": branch["_id"]})
        closing_update = db.inventory_updates.find_one({"_id": closing_oid, "branchId": branch["_id"]})

        if not opening_update or not closing_update:
            return json_response(404, {"message": "One or both inventory updates not found."})

        opening_time = opening_update.get("createdAt") or opening_update["submittedAt"]
        closing_time = closing_update.get("createdAt") or closing_update["submittedAt"]

        if closing_time <= opening_time:
            return json_response(400, {"message": "Closing update must be after opening update."})

        # Fetch full branch snapshots from inventory_snapshots collection
        opening_snap_doc = db.inventory_snapshots.find_one({"updateId": opening_oid})
        closing_snap_doc = db.inventory_snapshots.find_one({"updateId": closing_oid})

        if not opening_snap_doc or not closing_snap_doc:
            return json_response(400, {
                "message": "Snapshot data not found for one or both updates. Run the snapshot migration first.",
            })

        opening_snapshot = opening_snap_doc.get("items", [])
        closing_snapshot = closing_snap_doc.get("items", [])

        opening_by_item = {str(s["itemId"]): s for s in opening_snapshot}
        closing_by_item = {str(s["itemId"]): s for s in closing_snapshot}

        # Only use items present in BOTH snapshots (matched items)
        matched_item_ids = set(opening_by_item.keys()) & set(closing_by_item.keys())

        # Fetch all bills in the period for this branch (using billDate - the actual bill date,
        # not createdAt, since bills may be uploaded days later)
        bills_in_period = list(db.bills.find({
            "branchId": branch["_id"],
            "billDate": {"$gte": opening_time, "$lte": closing_time},
            "status": "confirmed",
        }))

        # Build purchase data per inventory item from bill line items
        # { itemId_str: [ { qty_base, price_per_base, total } ] }
        purchase_data = {}
        for bill in bills_in_period:
            for li in bill.get("lineItems", []):
                inv_item_id = str(li.get("inventoryItemId", ""))
                if not inv_item_id:
                    continue
                if inv_item_id not in purchase_data:
                    purchase_data[inv_item_id] = []
                purchase_data[inv_item_id].append({
                    "qtyBase": li.get("quantityInBaseUnit", 0),
                    "pricePerBase": li.get("pricePerBaseUnit", 0),
                    "total": li.get("total", 0),
                    "quantity": li.get("quantity", 0),
                    "unitPrice": li.get("unitPrice", 0),
                    "conversionFactor": li.get("conversionFactor", 1),
                })

        # Look up all matched items from DB for base prices
        matched_oids = [ObjectId(iid) for iid in matched_item_ids]
        item_docs = {str(d["_id"]): d for d in db.items.find({"_id": {"$in": matched_oids}})}

        # Calculate food cost per item
        item_results = []
        warnings = []
        total_opening = 0
        total_purchases = 0
        total_closing = 0

        for item_id_str in sorted(matched_item_ids):
            opening_item = opening_by_item[item_id_str]
            closing_item = closing_by_item[item_id_str]
            item_doc = item_docs.get(item_id_str, {})

            opening_qty_base = float(opening_item.get("quantityBase") or opening_item.get("quantity", 0))
            closing_qty_base = float(closing_item.get("quantityBase") or closing_item.get("quantity", 0))
            base_unit = opening_item.get("baseUnit") or opening_item.get("unit", "")

            # Resolve effective price
            purchases = purchase_data.get(item_id_str, [])
            price_source = "base_price"
            effective_price = 0
            purchase_qty_base = 0
            purchase_value = 0

            if purchases:
                # Weighted average price from bills
                total_qty = sum(p["qtyBase"] for p in purchases)
                if total_qty > 0:
                    weighted_sum = sum(p["qtyBase"] * p["pricePerBase"] for p in purchases)
                    effective_price = weighted_sum / total_qty
                    price_source = "bill_avg"
                purchase_qty_base = total_qty
                purchase_value = sum(p["total"] for p in purchases)

            if effective_price == 0:
                # Fall back to base price
                bp = item_doc.get("basePricePerUnit")
                if bp is not None and bp > 0:
                    effective_price = bp
                    price_source = "base_price"
                else:
                    # No price available at all
                    warnings.append({
                        "type": "no_base_price",
                        "itemId": item_id_str,
                        "itemName": item_doc.get("name", opening_item.get("name", "")),
                        "message": "No base price set and no bills found — excluded from calculation",
                    })
                    continue

            opening_value = opening_qty_base * effective_price
            closing_value = closing_qty_base * effective_price
            cost_contribution = opening_value + purchase_value - closing_value

            total_opening += opening_value
            total_purchases += purchase_value
            total_closing += closing_value

            # Look up category name
            cat_name = ""
            if item_doc.get("categoryId"):
                cat = db.categories.find_one({"_id": item_doc["categoryId"]})
                if cat:
                    cat_name = cat["name"]

            item_results.append({
                "itemId": item_id_str,
                "sku": item_doc.get("sku", opening_item.get("sku", "")),
                "name": item_doc.get("name", opening_item.get("name", "")),
                "category": cat_name,
                "openingQtyBase": opening_qty_base,
                "openingUnit": base_unit,
                "purchasedQtyBase": purchase_qty_base,
                "purchaseValue": round(purchase_value, 2),
                "closingQtyBase": closing_qty_base,
                "closingUnit": base_unit,
                "effectivePricePerBaseUnit": round(effective_price, 4),
                "priceSource": price_source,
                "openingValue": round(opening_value, 2),
                "closingValue": round(closing_value, 2),
                "costContribution": round(cost_contribution, 2),
            })

        food_cost_value = total_opening + total_purchases - total_closing

        # Format bills used
        bills_used = []
        for bill in bills_in_period:
            supplier_name = ""
            if bill.get("supplierId"):
                supplier = db.suppliers.find_one({"_id": bill["supplierId"]})
                if supplier:
                    supplier_name = supplier["name"]

            bills_used.append({
                "billId": str(bill["_id"]),
                "orderNo": bill.get("orderNo"),
                "billDate": bill.get("billDate"),
                "supplierName": supplier_name,
                "source": bill.get("source"),
                "grandTotal": bill.get("grandTotal"),
                "itemCount": len(bill.get("lineItems", [])),
                "hasFile": bool(bill.get("s3Key")),
            })

        return json_response(200, {
            "summary": {
                "openingValue": round(total_opening, 2),
                "purchaseValue": round(total_purchases, 2),
                "closingValue": round(total_closing, 2),
                "foodCostValue": round(food_cost_value, 2),
                "openingUpdate": {
                    "id": str(opening_update["_id"]),
                    "createdAt": opening_update.get("createdAt"),
                    "createdAtIST": _format_ist(opening_update.get("createdAt")),
                    "itemCount": opening_update.get("itemCount", 0),
                },
                "closingUpdate": {
                    "id": str(closing_update["_id"]),
                    "createdAt": closing_update.get("createdAt"),
                    "createdAtIST": _format_ist(closing_update.get("createdAt")),
                    "itemCount": closing_update.get("itemCount", 0),
                },
                "billCount": len(bills_in_period),
                "period": {
                    "from": opening_time,
                    "to": closing_time,
                },
            },
            "items": item_results,
            "billsUsed": bills_used,
            "warnings": warnings,
        })

    except Exception as exc:
        print("get_food_cost error", exc)
        return json_response(500, {"message": "Internal server error."})
