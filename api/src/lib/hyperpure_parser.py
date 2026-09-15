"""Parser for Zomato Hyperpure challan and invoice PDFs.

Uses pdfplumber to extract line items from challan and invoice tables.

Supports two PDF types:
  - Challan: single-page with category headers, pre-tax totals, discounts
  - Invoice: often a multi-page combined PDF holding both a TAX INVOICE
    (taxable goods) and a BILL OF SUPPLY (GST-exempt goods) for the same
    order. Every document page is parsed and the items merged. No
    pre-tax/discount columns; tax rate/amount use CGST+SGST+IGST format.

Exact-duplicate rows (same description, HSN, qty, unit price and total) are
collapsed to one and reported under duplicateLines. The same product at a
different qty or price stays as its own line.

The main entry point parse_hyperpure_pdf auto-detects the type.
"""

import io
import re

import pdfplumber


# Rows to exclude from line items
_EXCLUDED_DESCRIPTIONS = {"delivery charge", "delivery charges", "tcs u/s 206c(1h)", "small order charge"}
_EXCLUDED_HSN = {"996819", "999799"}
# Summary rows to skip entirely (not charges, just totals)
_SUMMARY_DESCRIPTIONS = {"total"}

# Serial-number header cells that mark the line-item table header row
_INVOICE_HEADER_LABELS = frozenset({"s no.", "s no", "sl no.", "sl no", "si no.", "si no", "si\nno."})

_CHALLAN_HEADER_LABELS = frozenset({"si no.", "si no", "sl no.", "sl no", "s.no", "s.no.", "si\nno."})

# A tax-rate cell ("2.5+2.5+0+0"). Unmistakable, and used to align rows on
# continuation pages that carry no header.
_TAX_RATE_TOKEN = re.compile(r"^\d+(?:\.\d+)?(?:\+\d+(?:\.\d+)?)+$")

# Category headers are rows where most cells are empty
_CATEGORY_HEADER_PATTERN = re.compile(
    r"^(bakery|chocolates|dairy|frozen|instant|fruits|vegetables|sauces|seasoning|"
    r"other charges|beverages|grocery|staples|packaging|disposable|cleaning|"
    r"meat|seafood|snacks|ready to|oils|ghee)",
    re.IGNORECASE,
)


def parse_hyperpure_pdf(pdf_bytes: bytes) -> dict:
    """Parse a Hyperpure PDF (auto-detects challan vs invoice).

    Args:
        pdf_bytes: Raw PDF file content.

    Returns:
        dict with keys: billMeta, lineItems, ignoredLines, totals
    """
    pdf = pdfplumber.open(io.BytesIO(pdf_bytes))

    if not pdf.pages:
        pdf.close()
        raise ValueError("PDF has no pages")

    # Auto-detect: any page carrying an invoice banner makes this an invoice PDF
    is_invoice = any(
        marker in ((page.extract_text() or "").upper())
        for page in pdf.pages
        for marker in ("TAX INVOICE", "BILL OF SUPPLY")
    )
    pdf.close()

    if is_invoice:
        return parse_hyperpure_invoice_pdf(pdf_bytes)

    return _parse_hyperpure_challan_pdf(pdf_bytes)


def _parse_hyperpure_challan_pdf(pdf_bytes: bytes) -> dict:
    """Parse a Hyperpure challan PDF and return structured bill data.

    A challan can run onto further pages, which repeat no column header, so
    rows there are aligned by their tax-rate cell instead of by a column map.
    """
    pdf = pdfplumber.open(io.BytesIO(pdf_bytes))

    if not pdf.pages:
        raise ValueError("PDF has no pages")

    # Metadata lives in the first page's header block
    bill_meta = _extract_meta(pdf.pages[0].extract_text() or "")

    line_items = []
    ignored_lines = []
    duplicate_lines = []
    seen_rows = set()
    sl_no = 0
    found_table = False
    col_map = None

    for page in pdf.pages:
        tables = page.extract_tables()
        if not tables:
            continue
        found_table = True

        # Find the main line items table (usually the largest one)
        main_table = max(tables, key=len)

        # Detect column layout from the header row. Continuation pages have no
        # header; they keep col_map as None so rows fall through to the
        # tax-rate-anchored parser below.
        page_col_map = None
        for row in main_table:
            if not row:
                continue
            cells = [str(c).strip().lower() if c else "" for c in row]
            if any(c in _CHALLAN_HEADER_LABELS for c in cells):
                page_col_map = _detect_column_map(cells)
                break
        col_map = page_col_map

        sl_no = _collect_challan_rows(
            main_table, col_map, sl_no,
            line_items, ignored_lines, duplicate_lines, seen_rows,
        )

    if not found_table:
        raise ValueError("No tables found in PDF")

    # Calculate totals (grandTotal includes non-inventory charges like delivery, TCS)
    items_total = sum(i.get("total", 0) for i in line_items)
    other_charges = sum(il.get("total", 0) for il in ignored_lines)
    totals = {
        "subtotal": sum(i.get("preTaxTotal", 0) for i in line_items),
        "totalDiscount": sum(i.get("discount", 0) for i in line_items),
        "taxableAmount": sum(i.get("taxableAmount", 0) for i in line_items),
        "taxAmount": sum(i.get("taxAmount", 0) for i in line_items),
        "itemsTotal": items_total,
        "otherCharges": other_charges,
        "grandTotal": items_total + other_charges,
    }

    pdf.close()

    return {
        "billMeta": bill_meta,
        "lineItems": line_items,
        "ignoredLines": ignored_lines,
        "duplicateLines": duplicate_lines,
        "totals": totals,
    }


def _collect_challan_rows(
    table: list[list],
    col_map: dict | None,
    sl_no: int,
    line_items: list[dict],
    ignored_lines: list[dict],
    duplicate_lines: list[dict],
    seen_rows: set,
) -> int:
    """Parse one challan page's table, appending in place. Returns the new slNo."""
    for row in table:
        if not row or all(not cell or not str(cell).strip() for cell in row):
            continue

        # Clean cells — keep None positions as empty strings
        cells = [str(c).strip() if c else "" for c in row]

        # Skip header row
        if cells[0].lower() in _CHALLAN_HEADER_LABELS:
            continue

        # Skip category header rows (single text spanning the row)
        non_empty = [c for c in cells if c]
        if len(non_empty) <= 2:
            combined = " ".join(non_empty)
            if _CATEGORY_HEADER_PATTERN.match(combined):
                continue
            # Also skip if it's clearly not a data row (no numbers)
            if not any(c.replace(".", "").replace(",", "").isdigit() for c in non_empty):
                continue

        # With a header use its column map; a continuation page has none, so
        # fall back to anchoring the row on its tax-rate cell.
        if col_map:
            parsed = _parse_line_item_row(cells, col_map)
        else:
            parsed = _parse_continuation_line_item_row(cells)
        if not parsed:
            continue

        description_lower = parsed["description"].lower().strip()

        # Skip summary rows (e.g. "Total") — not a charge, just a subtotal
        if description_lower in _SUMMARY_DESCRIPTIONS:
            continue

        # Dedup before routing, so a repeated document cannot double-count a
        # charge via ignoredLines either.
        fingerprint = _row_fingerprint(parsed)
        if fingerprint in seen_rows:
            duplicate_lines.append({
                "description": parsed["description"],
                "quantity": parsed.get("quantity"),
                "total": parsed.get("total", 0),
                "reason": "Duplicate of an earlier line",
            })
            continue
        seen_rows.add(fingerprint)

        if description_lower in _EXCLUDED_DESCRIPTIONS or parsed.get("hsnCode") in _EXCLUDED_HSN:
            ignored_lines.append({
                "description": parsed["description"],
                "total": parsed.get("total", 0),
                "reason": "Non-inventory charge",
            })
            continue

        sl_no += 1
        parsed["slNo"] = sl_no
        line_items.append(parsed)

    return sl_no


def _is_numeric_cell(cell: str) -> bool:
    try:
        float(cell.replace(",", "").replace(" ", "").strip())
        return True
    except ValueError:
        return False


def _parse_continuation_line_item_row(cells: list[str]) -> dict | None:
    """Parse a challan row on a page that carries no column header.

    A challan that overflows onto a second page repeats no header, and
    pdfplumber infers a different column count from the sparser content, so the
    first page's column map does not transfer (it produced 18 columns against
    the header page's 15 on a real bill). The tax-rate cell is unambiguous
    though, so it anchors the row and every other field is read as an offset
    from it. Rows with no such cell - the totals line, the tax summary, the
    declaration - simply fail to parse, which is the behaviour we want.
    """
    non_empty = [c for c in cells if c]

    anchor = None
    for i, c in enumerate(non_empty):
        if _TAX_RATE_TOKEN.match(re.sub(r"\s+", "", c)):
            anchor = i
            break

    # Need description/HSN/qty/price to the left and tax amount/total to the right.
    if anchor is None or anchor < 4 or anchor + 2 >= len(non_empty):
        return None

    def to_float(cell):
        try:
            return float(cell.replace(",", "").replace(" ", "").strip())
        except (ValueError, AttributeError):
            return 0.0

    taxable_amount = to_float(non_empty[anchor - 1])
    discount = to_float(non_empty[anchor - 2])
    pre_tax_total = to_float(non_empty[anchor - 3])

    # UoM is the only non-numeric field in that run. Charge rows (delivery,
    # transaction fee) carry no unit at all, which shifts everything left of
    # it by one.
    maybe_uom = non_empty[anchor - 4]
    if _is_numeric_cell(maybe_uom):
        uom = ""
        price_idx = anchor - 4
    else:
        uom = maybe_uom.strip()
        price_idx = anchor - 5

    if price_idx - 3 < 0:
        return None

    unit_price = to_float(non_empty[price_idx])
    quantity = to_float(non_empty[price_idx - 1])
    hsn_code = re.sub(r"\s+", "", non_empty[price_idx - 2])
    description = non_empty[price_idx - 3]

    if not description or len(description) < 3:
        return None

    tax_amount = to_float(non_empty[anchor + 1])
    total = to_float(non_empty[anchor + 2])

    if quantity == 0 and unit_price == 0 and total == 0:
        return None

    return {
        "description": description.strip(),
        "hsnCode": hsn_code if hsn_code else None,
        "quantity": quantity,
        "unitPrice": unit_price,
        "uom": uom if uom else None,
        "preTaxTotal": pre_tax_total,
        "discount": discount,
        "taxableAmount": taxable_amount,
        "taxRate": _parse_tax_rate(re.sub(r"\s+", "", non_empty[anchor])),
        "taxAmount": tax_amount,
        "total": total,
    }


def _extract_meta(text: str) -> dict:
    """Extract bill metadata from the full page text."""
    meta = {
        "orderNo": None,
        "invoiceDate": None,
        "orderDate": None,
        "supplier": "Zomato Hyperpure",
        "paymentStatus": "unpaid",
    }

    # Order No
    m = re.search(r"Order\s*No[.:]?\s*(ZHPHR\S+)", text, re.IGNORECASE)
    if m:
        meta["orderNo"] = m.group(1)

    # Dates may be on a separate line from labels.
    # Pattern: "Invoice Date ... Order Date ..." then "22 Jul 2026 21 Jul 2026 ..."
    date_pattern = r"(\d{1,2}\s+\w{3,9}\s+\d{4})"
    m = re.search(
        r"Invoice\s*Date.*?Order\s*Date.*?\n\s*" + date_pattern + r"\s+" + date_pattern,
        text, re.IGNORECASE,
    )
    if m:
        meta["invoiceDate"] = m.group(1)
        meta["orderDate"] = m.group(2)
    else:
        # Fallback: inline format
        m = re.search(r"Invoice\s*Date[:\s]*\n?\s*" + date_pattern, text, re.IGNORECASE)
        if m:
            meta["invoiceDate"] = m.group(1)
        m = re.search(r"Order\s*Date[:\s]*\n?\s*" + date_pattern, text, re.IGNORECASE)
        if m:
            meta["orderDate"] = m.group(1)

    # Payment Status
    m = re.search(r"Payment\s*Status[:\s]*(unpaid|paid)", text, re.IGNORECASE)
    if m:
        meta["paymentStatus"] = m.group(1).lower()

    return meta


def _detect_column_map(header_cells: list[str]) -> dict:
    """Detect column indices from the header row.

    Returns a dict mapping field names to column indices.
    """
    col_map = {}
    for i, cell in enumerate(header_cells):
        c = cell.lower().replace("\n", " ").strip()
        if c in ("si no.", "si no", "sl no.", "sl no", "s.no", "s.no."):
            col_map["si_no"] = i
        elif "description" in c:
            col_map["description"] = i
        elif c == "hsn":
            col_map["hsn"] = i
        elif "qty" in c:
            col_map["qty"] = i
        elif "unit price" in c:
            col_map["unit_price"] = i
        elif c == "uom" or c == "uom ":
            col_map["uom"] = i
        elif "pre tax" in c:
            col_map["pre_tax"] = i
        elif "discou" in c or "discount" in c:
            col_map["discount"] = i
        elif "taxable" in c:
            col_map["taxable"] = i
        elif "tax rate" in c:
            col_map["tax_rate"] = i
        elif "total tax" in c:
            col_map["tax_amount"] = i
        elif c == "total":
            col_map["total"] = i
    return col_map


def _parse_line_item_row(cells: list[str], col_map: dict | None = None) -> dict | None:
    """Try to parse a table row as a line item.

    Expected column order:
    SI No | Description | HSN | Inv. Qty | Unit Price | UoM |
    Pre Tax Total | Discount | Taxable Amount | Tax Rate | Total Tax | Total
    """

    def to_float(s):
        if not s:
            return 0.0
        s = s.replace(",", "").replace(" ", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0

    def cell_at(idx):
        if idx is not None and idx < len(cells):
            return cells[idx]
        return ""

    if col_map:
        # Use detected column positions
        description = cell_at(col_map.get("description"))

        # If description col is empty, try using col after si_no
        if not description and col_map.get("si_no") is not None:
            si_idx = col_map["si_no"]
            # Walk forward to find first non-empty cell after si_no
            for k in range(si_idx + 1, min(si_idx + 4, len(cells))):
                if cells[k]:
                    description = cells[k]
                    break

        if not description or len(description) < 3:
            return None

        hsn_raw = cell_at(col_map.get("hsn"))
        hsn_code = re.sub(r"\s+", "", hsn_raw)

        quantity = to_float(cell_at(col_map.get("qty")))
        unit_price = to_float(cell_at(col_map.get("unit_price")))
        uom = cell_at(col_map.get("uom")).strip()
        pre_tax_total = to_float(cell_at(col_map.get("pre_tax")))
        discount = to_float(cell_at(col_map.get("discount")))
        taxable_amount = to_float(cell_at(col_map.get("taxable")))
        tax_rate_str = cell_at(col_map.get("tax_rate")).strip()
        tax_amount = to_float(cell_at(col_map.get("tax_amount")))
        total = to_float(cell_at(col_map.get("total")))
    else:
        # Fallback: strip None/empty cells and use positional parsing
        non_empty = [c for c in cells if c]
        if len(non_empty) < 10:
            return None

        si_str = non_empty[0].replace(",", "").strip()
        if si_str and si_str.replace(".", "").isdigit():
            description = non_empty[1]
            remaining = non_empty[2:]
        else:
            description = non_empty[0]
            remaining = non_empty[1:]

        if not description or len(description) < 3:
            return None

        hsn_raw = remaining[0] if remaining else ""
        hsn_code = re.sub(r"\s+", "", hsn_raw)

        try:
            quantity = to_float(remaining[1])
            unit_price = to_float(remaining[2])
            uom = (remaining[3] or "").strip()
            pre_tax_total = to_float(remaining[4])
            discount = to_float(remaining[5])
            taxable_amount = to_float(remaining[6])
            tax_rate_str = (remaining[7] or "").strip()
            tax_amount = to_float(remaining[8])
            total = to_float(remaining[9])
        except IndexError:
            return None

    # Must have meaningful numeric data
    if quantity == 0 and unit_price == 0 and total == 0:
        return None

    # Parse tax rate string "2.5+2.5+0+0" into component rates
    tax_rate = _parse_tax_rate(tax_rate_str)

    return {
        "description": description.strip(),
        "hsnCode": hsn_code if hsn_code else None,
        "quantity": quantity,
        "unitPrice": unit_price,
        "uom": uom if uom else None,
        "preTaxTotal": pre_tax_total,
        "discount": discount,
        "taxableAmount": taxable_amount,
        "taxRate": tax_rate,
        "taxAmount": tax_amount,
        "total": total,
    }


def _row_fingerprint(item: dict) -> tuple:
    """Identity of a line item for exact-duplicate detection.

    Only an all-fields match counts as a duplicate: the same product bought
    twice on one bill at a different quantity or price is a real second line,
    not a duplicate. This catches a document that got repeated inside a
    combined PDF, which is the case that actually produces double-counting.
    """
    return (
        re.sub(r"\s+", " ", item.get("description", "")).strip().lower(),
        item.get("hsnCode"),
        item.get("quantity"),
        item.get("unitPrice"),
        item.get("total"),
    )


def _parse_tax_rate(rate_str: str) -> dict:
    """Parse tax rate string like '2.5+2.5+0+0' into component rates."""
    result = {"cgst": 0.0, "sgst": 0.0, "igst": 0.0, "cess": 0.0}

    if not rate_str:
        return result

    # Remove % sign if present
    rate_str = rate_str.replace("%", "").strip()

    parts = rate_str.split("+")
    keys = ["cgst", "sgst", "igst", "cess"]

    for i, key in enumerate(keys):
        if i < len(parts):
            try:
                result[key] = float(parts[i].strip())
            except ValueError:
                pass

    return result


# ---------------------------------------------------------------------------
# Invoice (TAX INVOICE) parser
# ---------------------------------------------------------------------------


def parse_hyperpure_invoice_pdf(pdf_bytes: bytes) -> dict:
    """Parse a Hyperpure invoice PDF and return structured bill data.

    A downloaded Hyperpure PDF is often a *combined* document: Zomato splits a
    single order into one TAX INVOICE (taxable goods) plus one BILL OF SUPPLY
    (GST-exempt goods such as fresh produce and meat), and a long document
    continues onto further pages without repeating its header.

    Every page is therefore parsed and the line items concatenated, with
    slNo renumbered continuously across the whole PDF. A page with no
    document banner is treated as a continuation of the previous document.

    Returns:
        dict with keys: billMeta, lineItems, ignoredLines, totals
    """
    pdf = pdfplumber.open(io.BytesIO(pdf_bytes))

    try:
        if not pdf.pages:
            raise ValueError("PDF has no pages")

        line_items: list[dict] = []
        ignored_lines: list[dict] = []
        duplicate_lines: list[dict] = []
        seen_rows: set = set()
        documents: list[dict] = []
        primary_meta = None
        fallback_meta = None
        current_doc_type = None
        current_col_map = None
        sl_no = 0

        for page in pdf.pages:
            text = page.extract_text() or ""
            upper = text.upper()

            if "TAX INVOICE" in upper:
                page_doc_type = "invoice"
            elif "BILL OF SUPPLY" in upper:
                page_doc_type = "bill_of_supply"
            else:
                # Continuation page of the document that started earlier.
                page_doc_type = None

            if page_doc_type is not None:
                current_doc_type = page_doc_type
                # A new document restarts the table, so re-detect its columns.
                current_col_map = None
                meta = _extract_invoice_meta(text)
                meta["documentType"] = page_doc_type
                documents.append({
                    "documentType": page_doc_type,
                    "invoiceNumber": meta.get("invoiceNumber"),
                })
                if fallback_meta is None:
                    fallback_meta = meta
                # The TAX INVOICE carries the canonical invoice number.
                if page_doc_type == "invoice" and primary_meta is None:
                    primary_meta = meta
            elif current_doc_type is None:
                # Cover page / anything before the first document banner.
                continue

            tables = page.extract_tables()
            if not tables:
                continue

            main_table = max(tables, key=len)

            detected = _find_invoice_col_map(main_table)
            if detected:
                current_col_map = detected

            sl_no = _collect_invoice_rows(
                main_table, current_col_map, current_doc_type,
                sl_no, line_items, ignored_lines, duplicate_lines, seen_rows,
            )

        if not documents:
            raise ValueError("No TAX INVOICE or BILL OF SUPPLY page found in PDF")

        bill_meta = dict(primary_meta or fallback_meta)
        bill_meta["documents"] = documents

        items_total = sum(i.get("total", 0) for i in line_items)
        other_charges = sum(il.get("total", 0) for il in ignored_lines)
        totals = {
            "taxableAmount": sum(i.get("taxableAmount", 0) for i in line_items),
            "taxAmount": sum(i.get("taxAmount", 0) for i in line_items),
            "itemsTotal": round(items_total, 2),
            "otherCharges": round(other_charges, 2),
            "grandTotal": round(items_total + other_charges, 2),
        }

        return {
            "billMeta": bill_meta,
            "lineItems": line_items,
            "ignoredLines": ignored_lines,
            "duplicateLines": duplicate_lines,
            "totals": totals,
        }
    finally:
        pdf.close()


def _find_invoice_col_map(table: list[list]) -> dict | None:
    """Return the column map from a table's header row, or None if absent."""
    for row in table:
        if not row:
            continue
        cells = [str(c).strip().lower() if c else "" for c in row]
        if any(c in _INVOICE_HEADER_LABELS for c in cells):
            return _detect_invoice_column_map(cells)
    return None


def _collect_invoice_rows(
    table: list[list],
    col_map: dict | None,
    doc_type: str | None,
    sl_no: int,
    line_items: list[dict],
    ignored_lines: list[dict],
    duplicate_lines: list[dict],
    seen: set,
) -> int:
    """Parse one page's table, appending results in place. Returns the new slNo."""
    for row in table:
        if not row or all(not cell or not str(cell).strip() for cell in row):
            continue

        cells = [str(c).strip() if c else "" for c in row]

        # Skip header row
        if cells[0].lower() in _INVOICE_HEADER_LABELS:
            continue

        # Skip section headers like "Other Charges"
        non_empty = [c for c in cells if c]
        if len(non_empty) <= 2:
            combined = " ".join(non_empty)
            if _CATEGORY_HEADER_PATTERN.match(combined):
                continue
            if not any(c.replace(".", "").replace(",", "").isdigit() for c in non_empty):
                continue

        parsed = _parse_invoice_line_item_row(cells, col_map)
        if not parsed:
            continue

        description_lower = parsed["description"].lower().strip()

        # Per-document "Total" row — a subtotal, not a charge.
        if description_lower in _SUMMARY_DESCRIPTIONS:
            continue

        # Dedup before routing, so a repeated document cannot double-count a
        # charge via ignoredLines either.
        fingerprint = _row_fingerprint(parsed)
        if fingerprint in seen:
            duplicate_lines.append({
                "description": parsed["description"],
                "quantity": parsed.get("quantity"),
                "total": parsed.get("total", 0),
                "reason": "Duplicate of an earlier line",
            })
            continue
        seen.add(fingerprint)

        if description_lower in _EXCLUDED_DESCRIPTIONS or parsed.get("hsnCode") in _EXCLUDED_HSN:
            ignored_lines.append({
                "description": parsed["description"],
                "total": parsed.get("total", 0),
                "reason": "Non-inventory charge",
            })
            continue

        sl_no += 1
        parsed["slNo"] = sl_no
        parsed["documentType"] = doc_type
        line_items.append(parsed)

    return sl_no


def _extract_invoice_meta(text: str) -> dict:
    """Extract bill metadata from an invoice page's text.

    The header layout is typically two lines:
      Invoice Number  Order No.  Invoice Date  Reference PO
      ZHPHR27-00179182  ZHPHR27-OR-0028523511  25 Jul 2026  -
    """
    meta = {
        "invoiceNumber": None,
        "orderNo": None,
        "invoiceDate": None,
        "supplier": "Zomato Hyperpure",
        "documentType": "invoice",
    }

    # Match the two-line header pattern
    m = re.search(
        r"Invoice\s*Number\s+Order\s*No\.?\s+Invoice\s*Date.*?\n"
        r"\s*(\S+)\s+(ZHPHR\S+)\s+(\d{1,2}\s+\w{3,9}\s+\d{4})",
        text, re.IGNORECASE,
    )
    if m:
        meta["invoiceNumber"] = m.group(1)
        meta["orderNo"] = m.group(2)
        meta["invoiceDate"] = m.group(3)
    else:
        # Fallback: try individual patterns
        m = re.search(r"\n\s*(Z[A-Z]PHR\d+-\d+)\s+", text)
        if m:
            meta["invoiceNumber"] = m.group(1)

        m = re.search(r"(ZHPHR\d+-OR-\d+)", text)
        if m:
            meta["orderNo"] = m.group(1)

        date_pattern = r"(\d{1,2}\s+\w{3,9}\s+\d{4})"
        m = re.search(r"Invoice\s*Date.*?" + date_pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            meta["invoiceDate"] = m.group(1)

    return meta


def _detect_invoice_column_map(header_cells: list[str]) -> dict:
    """Detect column indices from an invoice header row."""
    col_map = {}
    for i, cell in enumerate(header_cells):
        c = cell.lower().replace("\n", " ").strip()
        if c in _INVOICE_HEADER_LABELS:
            col_map["s_no"] = i
        elif "description" in c:
            col_map["description"] = i
        elif c == "hsn":
            col_map["hsn"] = i
        elif "qty" in c:
            col_map["qty"] = i
        elif "unit price" in c:
            col_map["unit_price"] = i
        elif c in ("uom", "uom "):
            col_map["uom"] = i
        elif "taxable" in c:
            col_map["taxable"] = i
        elif "tax rate" in c:
            col_map["tax_rate"] = i
        elif "tax amount" in c:
            col_map["tax_amount"] = i
        elif c == "total":
            col_map["total"] = i
    return col_map


def _parse_invoice_line_item_row(cells: list[str], col_map: dict | None = None) -> dict | None:
    """Parse an invoice table row as a line item.

    Invoice columns: S No. | Description | HSN | Qty | Unit Price | UoM |
    Taxable Amount | Tax Rate (CGST+SGS T+IGST)% | Tax Amount (CGST+SGS T+IGST) | Total
    """

    def to_float(s):
        if not s:
            return 0.0
        s = s.replace(",", "").replace(" ", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0

    def cell_at(idx):
        if idx is not None and idx < len(cells):
            return cells[idx]
        return ""

    if col_map:
        description = cell_at(col_map.get("description"))

        # If description col is empty, try walking forward from s_no
        if not description and col_map.get("s_no") is not None:
            si_idx = col_map["s_no"]
            for k in range(si_idx + 1, min(si_idx + 4, len(cells))):
                if cells[k]:
                    description = cells[k]
                    break

        if not description or len(description) < 3:
            return None

        hsn_raw = cell_at(col_map.get("hsn"))
        hsn_code = re.sub(r"\s+", "", hsn_raw)

        quantity = to_float(cell_at(col_map.get("qty")))
        unit_price = to_float(cell_at(col_map.get("unit_price")))
        uom = cell_at(col_map.get("uom")).strip()
        taxable_amount = to_float(cell_at(col_map.get("taxable")))
        tax_rate_str = cell_at(col_map.get("tax_rate")).strip()
        tax_amount_str = cell_at(col_map.get("tax_amount")).strip()
        total = to_float(cell_at(col_map.get("total")))
    else:
        # Fallback: positional parsing
        non_empty = [c for c in cells if c]
        if len(non_empty) < 8:
            return None

        si_str = non_empty[0].replace(",", "").strip()
        if si_str and si_str.replace(".", "").isdigit():
            description = non_empty[1]
            remaining = non_empty[2:]
        else:
            description = non_empty[0]
            remaining = non_empty[1:]

        if not description or len(description) < 3:
            return None

        hsn_raw = remaining[0] if remaining else ""
        hsn_code = re.sub(r"\s+", "", hsn_raw)

        try:
            quantity = to_float(remaining[1])
            unit_price = to_float(remaining[2])
            uom = (remaining[3] or "").strip()
            taxable_amount = to_float(remaining[4])
            tax_rate_str = (remaining[5] or "").strip()
            tax_amount_str = (remaining[6] or "").strip()
            total = to_float(remaining[7])
        except IndexError:
            return None

    # Must have meaningful numeric data
    if quantity == 0 and unit_price == 0 and total == 0:
        return None

    tax_rate = _parse_tax_rate(tax_rate_str)
    tax_amount = _parse_invoice_tax_amount(tax_amount_str)

    return {
        "description": description.strip(),
        "hsnCode": hsn_code if hsn_code else None,
        "quantity": quantity,
        "unitPrice": unit_price,
        "uom": uom if uom else None,
        "taxableAmount": taxable_amount,
        "taxRate": tax_rate,
        "taxAmount": tax_amount,
        "total": total,
    }


def _parse_invoice_tax_amount(amount_str: str) -> float:
    """Parse invoice tax amount string like '6.1+6.1+0' into a total."""
    if not amount_str:
        return 0.0

    parts = amount_str.split("+")
    total = 0.0
    for part in parts:
        try:
            total += float(part.strip())
        except ValueError:
            pass
    return round(total, 2)
