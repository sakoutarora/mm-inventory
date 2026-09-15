import json
import re
import uuid
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from src.lib.auth import extract_bearer_token, verify_token
from src.lib.mongo import get_db
from src.lib.response import json_response
from src.lib.multipart import parse_multipart_event
from src.lib.hyperpure_parser import parse_hyperpure_pdf
from src.lib.s3 import build_bill_s3_key, generate_presigned_upload_url, generate_presigned_download_url
from src.lib.unit_conversion import convert_to_base, canonical_unit


def handle_parse_hyperpure(event, _context):
    """POST /bills/parse-hyperpure — parse an uploaded Hyperpure PDF and suggest mappings."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            auth = verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        if auth.get("role") != "admin":
            return json_response(403, {"message": "Admin access required."})

        # Parse multipart form data to get the PDF file
        files = parse_multipart_event(event)
        pdf_bytes = files.get("file")
        if not pdf_bytes:
            return json_response(400, {"message": "No file uploaded. Send a PDF as 'file' field."})

        # Parse the PDF
        try:
            parsed = parse_hyperpure_pdf(pdf_bytes)
        except Exception as e:
            return json_response(422, {
                "message": f"Could not parse this PDF: {str(e)}. Please check the format or enter the bill manually.",
            })

        db = get_db()

        # Enrich line items with mapping suggestions
        for item in parsed["lineItems"]:
            suggestion = _find_mapping_suggestion(db, item["description"], item.get("hsnCode"))
            item["suggestedMapping"] = suggestion

        return json_response(200, parsed)

    except Exception as exc:
        print("parse_hyperpure error", exc)
        return json_response(500, {"message": "Internal server error."})


def handle_confirm_bill(event, _context):
    """POST /bills/confirm — save a confirmed bill with mappings."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            auth = verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        if auth.get("role") != "admin":
            return json_response(403, {"message": "Admin access required."})

        body = json.loads(event.get("body") or "{}")

        branch_code = (body.get("branchCode") or "").strip().upper()
        source = (body.get("source") or "generic").strip().lower()
        supplier_id = body.get("supplierId")
        supplier_name = (body.get("supplierName") or "").strip()
        order_no = (body.get("orderNo") or "").strip() or None
        bill_number = (body.get("billNumber") or "").strip() or None
        bill_date_str = (body.get("billDate") or "").strip()
        order_date_str = (body.get("orderDate") or "").strip() or None
        payment_status = (body.get("paymentStatus") or "unpaid").strip().lower()
        line_items = body.get("lineItems") or []
        grand_total = body.get("grandTotal", 0)
        other_charges = float(body.get("otherCharges", 0))
        s3_upload = body.get("s3Upload")

        if not branch_code:
            return json_response(400, {"message": "branchCode is required."})
        if not line_items:
            return json_response(400, {"message": "At least one line item is required."})
        if not bill_date_str:
            return json_response(400, {"message": "billDate is required."})

        db = get_db()

        # Resolve branch
        branch = db.branches.find_one({"code": branch_code, "isActive": True})
        if not branch:
            return json_response(404, {"message": "Branch not found."})

        # Duplicate detection for Hyperpure bills
        if source == "hyperpure" and order_no:
            existing_bill = db.bills.find_one({
                "branchId": branch["_id"],
                "source": "hyperpure",
                "orderNo": order_no,
            })
            if existing_bill:
                saved_date = ""
                if existing_bill.get("createdAt"):
                    saved_date = existing_bill["createdAt"].strftime("%d %b %Y")
                return json_response(409, {
                    "message": f"A bill with order number {order_no} already exists (saved on {saved_date}). Use the edit feature to modify it.",
                    "existingBillId": str(existing_bill["_id"]),
                })

        # Resolve supplier (case-insensitive lookup)
        supplier_oid = None
        if supplier_id:
            try:
                supplier_oid = ObjectId(supplier_id)
            except Exception:
                return json_response(400, {"message": "Invalid supplierId."})
        elif supplier_name:
            now = datetime.now(timezone.utc)
            existing = db.suppliers.find_one({
                "name": {"$regex": f"^{re.escape(supplier_name)}$", "$options": "i"}
            })
            if existing:
                supplier_oid = existing["_id"]
            else:
                try:
                    result = db.suppliers.insert_one({
                        "name": supplier_name,
                        "type": "generic",
                        "gstin": None,
                        "isActive": True,
                        "createdAt": now,
                        "updatedAt": now,
                    })
                    supplier_oid = result.inserted_id
                except DuplicateKeyError:
                    existing = db.suppliers.find_one({"name": {"$regex": f"^{re.escape(supplier_name)}$", "$options": "i"}})
                    supplier_oid = existing["_id"] if existing else None

        # Parse bill date
        try:
            bill_date = datetime.fromisoformat(bill_date_str.replace("Z", "+00:00"))
        except Exception:
            try:
                from datetime import date
                bill_date = datetime.strptime(bill_date_str, "%d %b %Y").replace(tzinfo=timezone.utc)
            except Exception:
                bill_date = datetime.now(timezone.utc)

        order_date = None
        if order_date_str:
            try:
                order_date = datetime.fromisoformat(order_date_str.replace("Z", "+00:00"))
            except Exception:
                try:
                    order_date = datetime.strptime(order_date_str, "%d %b %Y").replace(tzinfo=timezone.utc)
                except Exception:
                    pass

        now = datetime.now(timezone.utc)

        # Process line items and build bill document
        bill_line_items = []
        subtotal = 0
        total_discount = 0
        total_taxable = 0
        total_tax = 0

        for li in line_items:
            inventory_item_id = li.get("inventoryItemId")
            if not inventory_item_id:
                return json_response(400, {
                    "message": f"Line item '{li.get('description', '?')}' has no mapped inventory item.",
                })

            try:
                inv_item_oid = ObjectId(inventory_item_id)
            except Exception:
                return json_response(400, {"message": f"Invalid inventoryItemId: {inventory_item_id}"})

            # Look up inventory item
            inv_item = db.items.find_one({"_id": inv_item_oid})

            conv_factor = li.get("conversionFactor", 1)
            conv_from = li.get("conversionFromUnit", "")
            conv_to = li.get("conversionToUnit", "")

            qty = float(li.get("quantity", 0))
            unit_price = float(li.get("unitPrice", 0))
            discount = float(li.get("discount", 0))
            pre_tax = float(li.get("preTaxTotal", qty * unit_price))
            taxable = float(li.get("taxableAmount", pre_tax - discount))
            tax_amount = float(li.get("taxAmount", 0))
            total = float(li.get("total", taxable + tax_amount))

            # Calculate quantity in base unit and price per base unit
            try:
                factor = float(conv_factor) if conv_factor else 1
            except (TypeError, ValueError):
                factor = 1

            if factor <= 0:
                return json_response(400, {
                    "message": f"Conversion factor for '{li.get('description', '?')}' must be greater than 0.",
                })

            qty_in_base = qty * factor
            price_per_base = unit_price / factor

            subtotal += pre_tax
            total_discount += discount
            total_taxable += taxable
            total_tax += tax_amount

            bill_line_items.append({
                "slNo": li.get("slNo"),
                "description": li.get("description", ""),
                "hsnCode": li.get("hsnCode"),
                "quantity": qty,
                "unitPrice": unit_price,
                "uom": li.get("uom"),
                "preTaxTotal": pre_tax,
                "discount": discount,
                "taxableAmount": taxable,
                "taxRate": li.get("taxRate", {"cgst": 0, "sgst": 0, "igst": 0, "cess": 0}),
                "taxAmount": tax_amount,
                "total": total,
                "inventoryItemId": inv_item_oid,
                "inventoryItemName": inv_item["name"] if inv_item else "",
                "inventoryItemSku": inv_item["sku"] if inv_item else "",
                "conversionFactor": factor,
                "conversionFromUnit": conv_from,
                "conversionToUnit": conv_to,
                "quantityInBaseUnit": qty_in_base,
                "pricePerBaseUnit": price_per_base,
                "mappingSource": li.get("mappingSource", "manual"),
            })

            # Upsert global mapping if requested
            if li.get("saveGlobalMapping", True) and li.get("description"):
                db.bill_item_mappings.update_one(
                    {"billItemDescription": li["description"]},
                    {
                        "$set": {
                            "inventoryItemId": inv_item_oid,
                            "inventoryItemName": inv_item["name"] if inv_item else "",
                            "inventoryItemSku": inv_item["sku"] if inv_item else "",
                            "hsnCode": li.get("hsnCode"),
                            "conversionFactor": factor,
                            "conversionFromUnit": conv_from,
                            "conversionToUnit": conv_to,
                            "confirmedBy": ObjectId(auth["userId"]),
                            "updatedAt": now,
                        },
                        "$inc": {"confirmCount": 1},
                        "$setOnInsert": {"createdAt": now},
                    },
                    upsert=True,
                )

            # Save unit conversion to item for future reuse
            if factor != 1 and conv_from and inv_item:
                canon_from = canonical_unit(conv_from)
                default_unit = canonical_unit(inv_item.get("defaultUnit", ""))
                if canon_from and canon_from != default_unit:
                    db.items.update_one(
                        {"_id": inv_item_oid},
                        {"$set": {f"unitConversions.{canon_from}": factor}},
                    )

        # Handle S3 upload
        s3_key = None
        s3_presigned_url = None
        if s3_upload:
            filename = s3_upload.get("fileName", f"{uuid.uuid4().hex}.pdf")
            content_type = s3_upload.get("contentType", "application/pdf")
            s3_key = build_bill_s3_key(branch_code, bill_date_str, filename)
            s3_presigned_url = generate_presigned_upload_url(s3_key, content_type)

        bill_doc = {
            "branchId": branch["_id"],
            "branchCode": branch_code,
            "supplierId": supplier_oid,
            "source": source,
            "orderNo": order_no,
            "billNumber": bill_number,
            "billDate": bill_date,
            "orderDate": order_date,
            "s3Key": s3_key,
            "s3Bucket": "inventory-bills" if s3_key else None,
            "subtotal": subtotal,
            "totalDiscount": total_discount,
            "totalTaxableAmount": total_taxable,
            "totalTaxAmount": total_tax,
            "otherCharges": other_charges,
            "grandTotal": float(grand_total) if grand_total else subtotal - total_discount + total_tax + other_charges,
            "lineItems": bill_line_items,
            "paymentStatus": payment_status,
            "status": "confirmed",
            "createdBy": ObjectId(auth["userId"]),
            "createdAt": now,
            "updatedAt": now,
        }

        result = db.bills.insert_one(bill_doc)

        response = {
            "message": "Bill saved.",
            "billId": str(result.inserted_id),
            "itemCount": len(bill_line_items),
        }
        if s3_presigned_url:
            response["s3PresignedUploadUrl"] = s3_presigned_url

        return json_response(201, response)

    except Exception as exc:
        print("confirm_bill error", exc)
        return json_response(500, {"message": "Internal server error."})


def handle_get_bills(event, _context):
    """GET /bills — list bills with filters."""
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
        date_from = qs.get("dateFrom")
        date_to = qs.get("dateTo")
        supplier_id = qs.get("supplierId")
        source = qs.get("source")
        limit = min(int(qs.get("limit", 50)), 200)
        skip = int(qs.get("skip", 0))

        db = get_db()
        query = {}

        if branch_code:
            branch = db.branches.find_one({"code": branch_code})
            if branch:
                query["branchId"] = branch["_id"]

        if date_from or date_to:
            date_q = {}
            if date_from:
                try:
                    date_q["$gte"] = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                except Exception:
                    pass
            if date_to:
                try:
                    date_q["$lte"] = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                except Exception:
                    pass
            if date_q:
                query["billDate"] = date_q

        if supplier_id:
            try:
                query["supplierId"] = ObjectId(supplier_id)
            except Exception:
                pass

        if source and source in ("hyperpure", "generic"):
            query["source"] = source

        bills = list(
            db.bills.find(query)
            .sort("billDate", -1)
            .skip(skip)
            .limit(limit)
        )
        total_count = db.bills.count_documents(query)

        # Look up supplier names
        supplier_ids = list({b["supplierId"] for b in bills if b.get("supplierId")})
        suppliers = {str(s["_id"]): s["name"] for s in db.suppliers.find({"_id": {"$in": supplier_ids}})}

        return json_response(200, {
            "bills": [
                {
                    "id": str(b["_id"]),
                    "branchCode": b.get("branchCode"),
                    "supplierName": suppliers.get(str(b.get("supplierId")), ""),
                    "supplierId": str(b["supplierId"]) if b.get("supplierId") else None,
                    "source": b.get("source"),
                    "orderNo": b.get("orderNo"),
                    "billNumber": b.get("billNumber"),
                    "billDate": b.get("billDate"),
                    "grandTotal": b.get("grandTotal"),
                    "itemCount": len(b.get("lineItems", [])),
                    "paymentStatus": b.get("paymentStatus"),
                    "hasFile": bool(b.get("s3Key")),
                    "createdAt": b.get("createdAt"),
                }
                for b in bills
            ],
            "totalCount": total_count,
        })

    except Exception as exc:
        print("get_bills error", exc)
        return json_response(500, {"message": "Internal server error."})


def handle_get_bill_detail(event, _context):
    """GET /bills/:billId — get a single bill with full detail."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        path_params = event.get("pathParameters") or {}
        bill_id = path_params.get("billId")
        if not bill_id:
            return json_response(400, {"message": "billId is required."})

        try:
            bill_oid = ObjectId(bill_id)
        except Exception:
            return json_response(400, {"message": "Invalid billId."})

        db = get_db()
        bill = db.bills.find_one({"_id": bill_oid})
        if not bill:
            # Deleted bills stay viewable from the archive.
            bill = db.deleted_bills.find_one({"_id": bill_oid})
        if not bill:
            return json_response(404, {"message": "Bill not found."})

        # Get supplier name
        supplier_name = ""
        if bill.get("supplierId"):
            supplier = db.suppliers.find_one({"_id": bill["supplierId"]})
            if supplier:
                supplier_name = supplier["name"]

        # Generate download URL if file exists
        s3_download_url = None
        if bill.get("s3Key"):
            try:
                s3_download_url = generate_presigned_download_url(bill["s3Key"])
            except Exception:
                pass

        # Format line items
        formatted_items = []
        for li in bill.get("lineItems", []):
            formatted_items.append({
                "slNo": li.get("slNo"),
                "description": li.get("description"),
                "hsnCode": li.get("hsnCode"),
                "quantity": li.get("quantity"),
                "unitPrice": li.get("unitPrice"),
                "uom": li.get("uom"),
                "preTaxTotal": li.get("preTaxTotal"),
                "discount": li.get("discount"),
                "taxableAmount": li.get("taxableAmount"),
                "taxRate": li.get("taxRate"),
                "taxAmount": li.get("taxAmount"),
                "total": li.get("total"),
                "inventoryItemId": str(li["inventoryItemId"]) if li.get("inventoryItemId") else None,
                "inventoryItemName": li.get("inventoryItemName"),
                "inventoryItemSku": li.get("inventoryItemSku"),
                "conversionFactor": li.get("conversionFactor"),
                "conversionFromUnit": li.get("conversionFromUnit"),
                "conversionToUnit": li.get("conversionToUnit"),
                "quantityInBaseUnit": li.get("quantityInBaseUnit"),
                "pricePerBaseUnit": li.get("pricePerBaseUnit"),
            })

        return json_response(200, {
            "bill": {
                "id": str(bill["_id"]),
                "branchCode": bill.get("branchCode"),
                "supplierName": supplier_name,
                "supplierId": str(bill["supplierId"]) if bill.get("supplierId") else None,
                "source": bill.get("source"),
                "orderNo": bill.get("orderNo"),
                "billNumber": bill.get("billNumber"),
                "billDate": bill.get("billDate"),
                "orderDate": bill.get("orderDate"),
                "subtotal": bill.get("subtotal"),
                "totalDiscount": bill.get("totalDiscount"),
                "totalTaxableAmount": bill.get("totalTaxableAmount"),
                "totalTaxAmount": bill.get("totalTaxAmount"),
                "otherCharges": bill.get("otherCharges", 0),
                "grandTotal": bill.get("grandTotal"),
                "lineItems": formatted_items,
                "paymentStatus": bill.get("paymentStatus"),
                "createdAt": bill.get("createdAt"),
            },
            "s3DownloadUrl": s3_download_url,
        })

    except Exception as exc:
        print("get_bill_detail error", exc)
        return json_response(500, {"message": "Internal server error."})


def handle_get_bill_download_url(event, _context):
    """GET /bills/:billId/download-url — get a presigned download URL."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        path_params = event.get("pathParameters") or {}
        bill_id = path_params.get("billId")
        if not bill_id:
            return json_response(400, {"message": "billId is required."})

        try:
            bill_oid = ObjectId(bill_id)
        except Exception:
            return json_response(400, {"message": "Invalid billId."})

        db = get_db()
        bill = db.bills.find_one({"_id": bill_oid}, {"s3Key": 1})
        if not bill:
            # The S3 file is kept on delete, so archived bills stay downloadable.
            bill = db.deleted_bills.find_one({"_id": bill_oid}, {"s3Key": 1})
        if not bill:
            return json_response(404, {"message": "Bill not found."})

        if not bill.get("s3Key"):
            return json_response(404, {"message": "No file attached to this bill."})

        url = generate_presigned_download_url(bill["s3Key"])
        return json_response(200, {"url": url})

    except Exception as exc:
        print("get_bill_download_url error", exc)
        return json_response(500, {"message": "Internal server error."})


def handle_update_bill(event, _context):
    """PATCH /bills/:billId - update an existing confirmed bill."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            auth = verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        if auth.get("role") != "admin":
            return json_response(403, {"message": "Admin access required."})

        path_params = event.get("pathParameters") or {}
        bill_id = path_params.get("billId")
        if not bill_id:
            return json_response(400, {"message": "billId is required."})

        try:
            bill_oid = ObjectId(bill_id)
        except Exception:
            return json_response(400, {"message": "Invalid billId."})

        db = get_db()
        existing_bill = db.bills.find_one({"_id": bill_oid})
        if not existing_bill:
            return json_response(404, {"message": "Bill not found."})

        body = json.loads(event.get("body") or "{}")
        line_items = body.get("lineItems") or []
        supplier_name = (body.get("supplierName") or "").strip()
        bill_date_str = (body.get("billDate") or "").strip()
        bill_number = (body.get("billNumber") or "").strip() or None
        payment_status = (body.get("paymentStatus") or "").strip().lower() or None
        grand_total = body.get("grandTotal")
        other_charges = float(body.get("otherCharges", 0))

        if not line_items:
            return json_response(400, {"message": "At least one line item is required."})

        now = datetime.now(timezone.utc)

        # Resolve supplier if changed
        supplier_oid = existing_bill.get("supplierId")
        if supplier_name:
            existing_supplier = db.suppliers.find_one({
                "name": {"$regex": f"^{re.escape(supplier_name)}$", "$options": "i"}
            })
            if existing_supplier:
                supplier_oid = existing_supplier["_id"]
            else:
                try:
                    result = db.suppliers.insert_one({
                        "name": supplier_name,
                        "type": "generic",
                        "gstin": None,
                        "isActive": True,
                        "createdAt": now,
                        "updatedAt": now,
                    })
                    supplier_oid = result.inserted_id
                except DuplicateKeyError:
                    found = db.suppliers.find_one({"name": {"$regex": f"^{re.escape(supplier_name)}$", "$options": "i"}})
                    supplier_oid = found["_id"] if found else supplier_oid

        # Parse bill date if provided
        bill_date = existing_bill.get("billDate")
        if bill_date_str:
            try:
                bill_date = datetime.fromisoformat(bill_date_str.replace("Z", "+00:00"))
            except Exception:
                try:
                    bill_date = datetime.strptime(bill_date_str, "%d %b %Y").replace(tzinfo=timezone.utc)
                except Exception:
                    pass

        # Process line items
        bill_line_items = []
        subtotal = 0
        total_discount = 0
        total_taxable = 0
        total_tax = 0

        for li in line_items:
            inventory_item_id = li.get("inventoryItemId")
            if not inventory_item_id:
                return json_response(400, {
                    "message": f"Line item '{li.get('description', '?')}' has no mapped inventory item.",
                })

            try:
                inv_item_oid = ObjectId(inventory_item_id)
            except Exception:
                return json_response(400, {"message": f"Invalid inventoryItemId: {inventory_item_id}"})

            inv_item = db.items.find_one({"_id": inv_item_oid})

            conv_factor = li.get("conversionFactor", 1)
            conv_from = li.get("conversionFromUnit", "")
            conv_to = li.get("conversionToUnit", "")

            qty = float(li.get("quantity", 0))
            unit_price = float(li.get("unitPrice", 0))
            discount = float(li.get("discount", 0))
            pre_tax = float(li.get("preTaxTotal", qty * unit_price))
            taxable = float(li.get("taxableAmount", pre_tax - discount))
            tax_amount = float(li.get("taxAmount", 0))
            total = float(li.get("total", taxable + tax_amount))

            try:
                factor = float(conv_factor) if conv_factor else 1
            except (TypeError, ValueError):
                factor = 1

            if factor <= 0:
                return json_response(400, {
                    "message": f"Conversion factor for '{li.get('description', '?')}' must be greater than 0.",
                })

            qty_in_base = qty * factor
            price_per_base = unit_price / factor

            subtotal += pre_tax
            total_discount += discount
            total_taxable += taxable
            total_tax += tax_amount

            bill_line_items.append({
                "slNo": li.get("slNo"),
                "description": li.get("description", ""),
                "hsnCode": li.get("hsnCode"),
                "quantity": qty,
                "unitPrice": unit_price,
                "uom": li.get("uom"),
                "preTaxTotal": pre_tax,
                "discount": discount,
                "taxableAmount": taxable,
                "taxRate": li.get("taxRate", {"cgst": 0, "sgst": 0, "igst": 0, "cess": 0}),
                "taxAmount": tax_amount,
                "total": total,
                "inventoryItemId": inv_item_oid,
                "inventoryItemName": inv_item["name"] if inv_item else "",
                "inventoryItemSku": inv_item["sku"] if inv_item else "",
                "conversionFactor": factor,
                "conversionFromUnit": conv_from,
                "conversionToUnit": conv_to,
                "quantityInBaseUnit": qty_in_base,
                "pricePerBaseUnit": price_per_base,
                "mappingSource": li.get("mappingSource", "manual"),
            })

            # Upsert global mapping
            if li.get("saveGlobalMapping", True) and li.get("description"):
                db.bill_item_mappings.update_one(
                    {"billItemDescription": li["description"]},
                    {
                        "$set": {
                            "inventoryItemId": inv_item_oid,
                            "inventoryItemName": inv_item["name"] if inv_item else "",
                            "inventoryItemSku": inv_item["sku"] if inv_item else "",
                            "hsnCode": li.get("hsnCode"),
                            "conversionFactor": factor,
                            "conversionFromUnit": conv_from,
                            "conversionToUnit": conv_to,
                            "confirmedBy": ObjectId(auth["userId"]),
                            "updatedAt": now,
                        },
                        "$inc": {"confirmCount": 1},
                        "$setOnInsert": {"createdAt": now},
                    },
                    upsert=True,
                )

            # Save unit conversion to item
            if factor != 1 and conv_from and inv_item:
                canon_from = canonical_unit(conv_from)
                default_unit = canonical_unit(inv_item.get("defaultUnit", ""))
                if canon_from and canon_from != default_unit:
                    db.items.update_one(
                        {"_id": inv_item_oid},
                        {"$set": {f"unitConversions.{canon_from}": factor}},
                    )

        # Update bill document
        update_fields = {
            "lineItems": bill_line_items,
            "subtotal": subtotal,
            "totalDiscount": total_discount,
            "totalTaxableAmount": total_taxable,
            "totalTaxAmount": total_tax,
            "otherCharges": other_charges,
            "grandTotal": float(grand_total) if grand_total else subtotal - total_discount + total_tax + other_charges,
            "supplierId": supplier_oid,
            "billDate": bill_date,
            "updatedAt": now,
            "updatedBy": ObjectId(auth["userId"]),
        }
        if bill_number:
            update_fields["billNumber"] = bill_number
        if payment_status:
            update_fields["paymentStatus"] = payment_status

        db.bills.update_one({"_id": bill_oid}, {"$set": update_fields})

        return json_response(200, {
            "message": "Bill updated.",
            "billId": str(bill_oid),
            "itemCount": len(bill_line_items),
        })

    except Exception as exc:
        print("update_bill error", exc)
        return json_response(500, {"message": "Internal server error."})


def _find_mapping_suggestion(db, description: str, hsn_code: str | None) -> dict:
    """Find a mapping suggestion for a bill line item description."""
    # 1. Exact match in bill_item_mappings
    exact = db.bill_item_mappings.find_one({"billItemDescription": description})
    if exact:
        return {
            "inventoryItemId": str(exact["inventoryItemId"]),
            "inventoryItemName": exact.get("inventoryItemName"),
            "confidence": "high",
            "source": "exact",
            "existingConversion": {
                "factor": exact.get("conversionFactor"),
                "fromUnit": exact.get("conversionFromUnit"),
                "toUnit": exact.get("conversionToUnit"),
            } if exact.get("conversionFactor") else None,
        }

    # 2. Fuzzy match against inventory items
    try:
        from rapidfuzz import fuzz

        items = list(db.items.find({"isActive": True}, {"name": 1, "sku": 1}))
        best_score = 0
        best_item = None

        for item in items:
            score = fuzz.token_sort_ratio(description.lower(), item["name"].lower())
            if score > best_score:
                best_score = score
                best_item = item

        if best_item and best_score >= 65:
            confidence = "high" if best_score >= 85 else "medium"
            return {
                "inventoryItemId": str(best_item["_id"]),
                "inventoryItemName": best_item["name"],
                "confidence": confidence,
                "source": "fuzzy",
                "existingConversion": None,
            }
    except ImportError:
        pass

    # 3. HSN code match
    if hsn_code:
        hsn_mapping = db.bill_item_mappings.find_one({"hsnCode": hsn_code})
        if hsn_mapping:
            return {
                "inventoryItemId": str(hsn_mapping["inventoryItemId"]),
                "inventoryItemName": hsn_mapping.get("inventoryItemName"),
                "confidence": "medium",
                "source": "hsn",
                "existingConversion": {
                    "factor": hsn_mapping.get("conversionFactor"),
                    "fromUnit": hsn_mapping.get("conversionFromUnit"),
                    "toUnit": hsn_mapping.get("conversionToUnit"),
                } if hsn_mapping.get("conversionFactor") else None,
            }

    return {
        "inventoryItemId": None,
        "inventoryItemName": None,
        "confidence": "none",
        "source": "none",
        "existingConversion": None,
    }


def handle_delete_bill(event, _context):
    """DELETE /bills/:billId — move a bill into the deleted_bills collection."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            auth = verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        if auth.get("role") != "admin":
            return json_response(403, {"message": "Admin access required."})

        path_params = event.get("pathParameters") or {}
        bill_id = path_params.get("billId")
        if not bill_id:
            return json_response(400, {"message": "billId is required."})

        try:
            bill_oid = ObjectId(bill_id)
        except Exception:
            return json_response(400, {"message": "Invalid billId."})

        db = get_db()
        bill = db.bills.find_one({"_id": bill_oid})
        if not bill:
            return json_response(404, {"message": "Bill not found."})

        now = datetime.now(timezone.utc)
        archived = dict(bill)
        archived["status"] = "deleted"
        archived["deletedAt"] = now
        archived["deletedBy"] = ObjectId(auth["userId"])

        # Insert into the archive first, so a failure here leaves the bill intact.
        try:
            db.deleted_bills.insert_one(archived)
        except DuplicateKeyError:
            # Already archived by an earlier attempt that failed before the delete.
            pass

        db.bills.delete_one({"_id": bill_oid})

        return json_response(200, {
            "message": "Bill deleted.",
            "billId": str(bill_oid),
        })

    except Exception as exc:
        print("delete_bill error", exc)
        return json_response(500, {"message": "Internal server error."})


def handle_get_deleted_bills(event, _context):
    """GET /bills/deleted — list bills that were moved to the archive."""
    try:
        token = extract_bearer_token(event)
        if not token:
            return json_response(401, {"message": "Missing bearer token."})

        try:
            auth = verify_token(token)
        except Exception:
            return json_response(401, {"message": "Invalid token."})

        if auth.get("role") != "admin":
            return json_response(403, {"message": "Admin access required."})

        qs = event.get("queryStringParameters") or {}
        branch_code = (qs.get("branchCode") or "").strip().upper()
        date_from = qs.get("dateFrom")
        date_to = qs.get("dateTo")
        supplier_id = qs.get("supplierId")
        source = qs.get("source")
        limit = min(int(qs.get("limit", 50)), 200)
        skip = int(qs.get("skip", 0))

        db = get_db()
        query = {}

        if branch_code:
            branch = db.branches.find_one({"code": branch_code})
            if branch:
                query["branchId"] = branch["_id"]

        if date_from or date_to:
            date_q = {}
            if date_from:
                try:
                    date_q["$gte"] = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                except Exception:
                    pass
            if date_to:
                try:
                    date_q["$lte"] = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                except Exception:
                    pass
            if date_q:
                query["billDate"] = date_q

        if supplier_id:
            try:
                query["supplierId"] = ObjectId(supplier_id)
            except Exception:
                pass

        if source and source in ("hyperpure", "generic"):
            query["source"] = source

        bills = list(
            db.deleted_bills.find(query)
            .sort("deletedAt", -1)
            .skip(skip)
            .limit(limit)
        )
        total_count = db.deleted_bills.count_documents(query)

        supplier_ids = list({b["supplierId"] for b in bills if b.get("supplierId")})
        suppliers = {str(s["_id"]): s["name"] for s in db.suppliers.find({"_id": {"$in": supplier_ids}})}

        deleter_ids = list({b["deletedBy"] for b in bills if b.get("deletedBy")})
        deleters = {
            str(u["_id"]): u.get("fullName") or u.get("username") or ""
            for u in db.users.find({"_id": {"$in": deleter_ids}}, {"fullName": 1, "username": 1})
        }

        return json_response(200, {
            "bills": [
                {
                    "id": str(b["_id"]),
                    "branchCode": b.get("branchCode"),
                    "supplierName": suppliers.get(str(b.get("supplierId")), ""),
                    "supplierId": str(b["supplierId"]) if b.get("supplierId") else None,
                    "source": b.get("source"),
                    "orderNo": b.get("orderNo"),
                    "billNumber": b.get("billNumber"),
                    "billDate": b.get("billDate"),
                    "grandTotal": b.get("grandTotal"),
                    "itemCount": len(b.get("lineItems", [])),
                    "paymentStatus": b.get("paymentStatus"),
                    "hasFile": bool(b.get("s3Key")),
                    "createdAt": b.get("createdAt"),
                    "deletedAt": b.get("deletedAt"),
                    "deletedByName": deleters.get(str(b.get("deletedBy")), ""),
                }
                for b in bills
            ],
            "totalCount": total_count,
        })

    except Exception as exc:
        print("get_deleted_bills error", exc)
        return json_response(500, {"message": "Internal server error."})
