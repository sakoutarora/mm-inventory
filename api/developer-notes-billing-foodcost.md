# Developer Notes: Billing & Food Cost Module

## Product Spec Reference

This document contains UI/UX guidance and implementation notes for the Billing & Food Cost feature set. All features are **admin-only, console-only, branch-aware**.

---

## Console UI Structure — New Sections

Add three new tabs/sections to the console sidebar:

```
Existing:
  - Inventory (stock entry)
  - Dashboard (analytics)
  - Admin (items, categories, users)

New:
  - Bills          ← bill upload, manual entry, bill history
  - Food Cost      ← cost calculation between two inventory snapshots
  - Item Pricing   ← base price management per item
```

---

## 1. Bills Section

### 1A. Bill Upload — Hyperpure (Two-Step Flow)

**Step 1: Upload & Parse**

- Prominent **drag-and-drop zone** with "Upload Hyperpure Challan (PDF)" label
- Also a "Browse Files" button inside the drop zone
- On upload → call `POST /bills/parse-hyperpure` → show a **loading spinner** with "Parsing challan..."
- On success → transition to the **Review & Map** screen (Step 2)
- On parse failure → show inline error: "Could not parse this PDF. Please check the format or enter the bill manually."

**UI keywords:** drag-drop, file-upload, progress-indicator, inline-error

**Step 2: Review & Map Parsed Bill**

Layout: **two-panel or stacked layout**

**Top section — Bill metadata (auto-filled, editable):**
- Order No (read-only, from challan)
- Invoice Date / Order Date (editable date pickers)
- Supplier: auto-filled as "Zomato Hyperpure" (read-only for HP bills)
- Payment Status (dropdown: unpaid/paid)
- Branch (pre-selected from current context)

**Main section — Line items table:**

| # | Bill Description | HSN | Qty | Unit Price | UoM | Discount | Taxable Amt | Tax % | Tax Amt | Total | Mapped To | Action |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Paras - Makhani Dairy Paneer, 1 Kg | 04061000 | 2 | 308 | Pack | 0 | 616 | 0% | 0 | 616 | **Paneer** ✅ | Change / Confirm |
| 2 | Fresh 2 Go - Frozen 10" Tortilla... | 19059090 | 3 | 161 | Pack | 3 | 480 | 0% | 0 | 480 | **Tortilla Wrap** ⚠️ | Change / Confirm |
| 3 | Makino - Cheese Nacho Chips, 200gm | 21069099 | 7 | 62 | Count | 7 | 427 | 5% | 21.35 | 448.35 | **Not Found** ❌ | Create / Map |

**Mapping states:**
- ✅ **Auto-matched (high confidence):** Green background. Previously confirmed mapping exists in `bill_item_mappings`. Pre-selected, user can change.
- ⚠️ **Suggested (fuzzy match):** Yellow/amber background. Algorithm found a likely match but no prior confirmation. User must confirm or change.
- ❌ **Not found:** Red/pink background. No match found. User must either search & map to an existing item, or create a new inventory item.

**Mapping interaction:**
- Clicking "Change" opens a **searchable dropdown** of all inventory items. Type-ahead search by name or SKU.
- Clicking "Create" opens the **full item creation modal** (SKU, name, category, defaultUnit, allowedUnits, minThreshold, isRequired). On save, auto-maps the bill line to the new item.

**Conversion prompt:**
- When bill UoM ≠ inventory item's base unit family, show an **inline conversion row** below the line item:
  ```
  ┌─────────────────────────────────────────────────────────┐
  │ ⚠️ Unit mismatch: Bill says "Pack", inventory uses "pcs" │
  │                                                          │
  │ 1 Pack = [____] pcs    (e.g., 12)                       │
  │                                                          │
  │ Previously saved: 1 Pack = 12 pcs  [Use this]           │
  └─────────────────────────────────────────────────────────┘
  ```
- If a conversion already exists in `bill_item_mappings`, show it as a suggestion with a "Use this" button.
- User can type a new conversion factor.
- This conversion is saved globally on confirm (unless overridden per bill).

**Bottom section:**
- **Bill summary:** Subtotal, Total Tax, Grand Total (auto-summed from line items)
- **Ignored lines:** Show "Delivery Charge" and "TCS" as greyed-out rows with a note "Non-inventory charges — excluded from food cost"
- **"Confirm & Save Bill"** button (primary, prominent)
- **"Cancel"** button (secondary)

**UI keywords:** data-table, inline-editing, type-ahead-search, modal, status-badges, conversion-prompt, summary-footer

---

### 1B. Generic Bill Entry (Manual)

**Trigger:** "Add Generic Bill" button alongside the HP upload zone, or a tab toggle: `[Upload Hyperpure] [Enter Manually]`

**Bill header form:**
- Supplier: **searchable dropdown** of existing suppliers + "Add New Supplier" option at the bottom
  - "Add New Supplier" inline: just a name field + save
- Bill Date: date picker (defaults to today)
- Bill Number: text input (optional)
- Branch: pre-selected from context

**Line items — dynamic form rows:**

Each row:
```
[Item Name (text)] [Qty (number)] [Unit Price (number)] [Discount (number, default 0)]
[Tax: dropdown (0% | 5% | 12% | 18% | 28% | Custom)]  [Tax Amount (auto-calculated, editable)]
[Total (auto-calculated, read-only)]
[+ mapped inventory item selector]
```

- **"+ Add Item"** button below the last row
- **Remove row** (trash icon on each row)
- Tax dropdown: selecting a slab auto-calculates `taxAmount = taxableAmount × rate`. Selecting "Custom" makes the tax amount field fully editable.
- `taxableAmount = (qty × unitPrice) - discount`
- `total = taxableAmount + taxAmount`

**Mapping flow:** Same as HP bills — after filling all rows, each item goes through the mapping flow (fuzzy match against inventory, confirm/change/create).

**Bottom:**
- Auto-summed totals
- **"Confirm & Save Bill"** button

**UI keywords:** dynamic-form, repeater-rows, auto-calculate, dropdown-with-create, inline-validation

---

### 1C. Bill History View

**Layout:** Filterable table/list

**Filters bar:**
- Date range picker (default: last 30 days)
- Supplier dropdown (multi-select)
- Source toggle: All | Hyperpure | Generic
- Branch selector

**Table columns:**

| Date | Bill # / Order # | Supplier | Source | Items | Total (₹) | Actions |
|---|---|---|---|---|---|---|
| 17 Jul 2026 | ZHPHR27-OR-002825... | Hyperpure | HP | 9 items | ₹3,443.47 | View / Download |
| 15 Jul 2026 | MANDI-001 | Azadpur Mandi | Manual | 4 items | ₹2,100 | View |

- **"View"** expands inline or opens a detail panel showing all line items with mappings
- **"Download"** generates S3 presigned URL and triggers download (only for bills with uploaded files)

**UI keywords:** data-table, filters-bar, date-range-picker, expandable-rows, presigned-download

---

## 2. Food Cost Section

### 2A. Snapshot Picker

**Layout:** Two-column or side-by-side picker

```
┌──────────────────────────┐   ┌──────────────────────────┐
│ 📋 Opening Stock          │   │ 📋 Closing Stock          │
│                          │   │                          │
│ Select inventory update: │   │ Select inventory update: │
│ ┌──────────────────────┐ │   │ ┌──────────────────────┐ │
│ │ 21 Jul 2026, 9:15 AM │ │   │ │ 22 Jul 2026, 9:30 AM │ │
│ │ by: blr_staff        │ │   │ │ by: blr_staff        │ │
│ │ 18 items updated     │ │   │ │ 20 items updated     │ │
│ └──────────────────────┘ │   │ └──────────────────────┘ │
│ │ 21 Jul 2026, 6:00 PM │ │   │ │ ...                  │ │
│ │ by: admin             │ │   │ │                      │ │
│ └──────────────────────┘ │   │ └──────────────────────┘ │
└──────────────────────────┘   └──────────────────────────┘
```

- List of inventory update snapshots grouped by date, sorted newest first
- Each entry shows: timestamp, submitted by, item count
- Branch selector at the top
- Closing update must be after opening update (validate)
- **"Calculate Food Cost"** button (disabled until both selected)

**UI keywords:** list-selector, grouped-by-date, timestamp-display, validation, dual-picker

### 2B. Food Cost Results

**Top KPI cards row:**
```
┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
│ Opening Value  │ │ + Purchases    │ │ Closing Value  │ │ = Food Cost    │
│ ₹42,500        │ │ ₹8,640         │ │ ₹38,200        │ │ ₹12,940        │
│                │ │ (3 bills)      │ │                │ │                │
└────────────────┘ └────────────────┘ └────────────────┘ └────────────────┘
```

Visual formula: `Opening + Purchases - Closing = Food Cost` shown as a flow/equation with arrows between cards.

**Pricing logic note (for developers):**
```
For each item, resolve the "effective price" for this window:

1. Collect all bills between opening_update.submittedAt and closing_update.submittedAt
   that have a confirmed mapping to this inventory item.

2. If bills found for this item:
   → weighted_avg_price = Σ(bill_qty × bill_unit_price) / Σ(bill_qty)
   → Convert to base unit using the mapping's conversionFactor
   → e.g., 5 packs @ ₹161/pack, conversion 1 pack = 12 pcs
         → price per pc = ₹161 / 12 = ₹13.42/pc

3. If NO bills found for this item in this window:
   → Use item.basePricePerUnit (from items collection)

4. Apply same resolved price to BOTH opening and closing qty for this item
   → opening_value = opening_qty_base × effective_price_per_base_unit
   → closing_value = closing_qty_base × effective_price_per_base_unit
```

**Item breakdown table:**

| Item | Category | Opening Qty | Purchased Qty | Closing Qty | Effective Price (per unit) | Opening Value (₹) | Purchase Value (₹) | Closing Value (₹) | Cost (₹) |
|---|---|---|---|---|---|---|---|---|---|
| Paneer | Dairy | 5 kg | 2 kg | 3 kg | ₹308/kg | 1,540 | 616 | 924 | 1,232 |
| Tomato | Vegetables | 8 kg | 0 | 5 kg | ₹40/kg* | 320 | 0 | 200 | 120 |

- Items with `*` next to price → using base price (no bills in window). Show a subtle tooltip: "Using base price — no purchase bills found in this period"
- Items with purchase data → show bill-derived price, no asterisk
- Sortable columns
- Totals row at the bottom

**Bills Used section (below the table):**

```
Bills included in this calculation:
┌─────────────────────────────────────────────────────────────────────┐
│ 📄 ZHPHR27-OR-0028250710  |  17 Jul 2026  |  Hyperpure  |  ₹3,443  │
│    9 items  •  [View Details] [Download PDF]                        │
├─────────────────────────────────────────────────────────────────────┤
│ 📝 MANDI-001  |  18 Jul 2026  |  Azadpur Mandi  |  ₹2,100         │
│    4 items  •  [View Details]                                       │
└─────────────────────────────────────────────────────────────────────┘

[+ Add Bill]  ← opens bill upload/entry flow inline
```

- **"+ Add Bill"** button lets user upload an HP challan or enter a generic bill without leaving the food cost view. After saving, the calculation auto-refreshes.

**UI keywords:** kpi-cards, visual-formula, breakdown-table, sortable-columns, tooltip, expandable-bills-list, inline-add

---

## 3. Item Pricing Section

**Layout:** Editable table of all inventory items with their base prices

| SKU | Item Name | Category | Default Unit | Base Price (per default unit) | Base Price (per base unit) | Last Updated | Action |
|---|---|---|---|---|---|---|---|
| DAI-PANEER | Paneer | Dairy | kg | ₹308.00 | ₹0.308/gm | 15 Jul 2026 | Edit |
| VEG-TOMATO | Tomato | Vegetables | kg | ₹40.00 | ₹0.040/gm | 10 Jul 2026 | Edit |
| VEG-TORTILLA | Tortilla Wrap | Frozen | pcs | ₹13.42 | ₹13.42/pc | — | Edit |

- **"Edit"** makes the "Base Price (per default unit)" cell editable inline. System auto-converts to base unit on save.
- Search/filter by name, SKU, or category
- Items without a base price → highlighted row with "Not set" badge
- Bulk edit option (nice to have, not MVP)

**UI keywords:** inline-editable-table, search-filter, unit-conversion-display, status-badge

---

## 4. UI/UX Principles for This Module

### Color Coding & Status
- **Green (✅):** Confirmed, matched, complete
- **Amber/Yellow (⚠️):** Needs attention, suggested match, unconfirmed
- **Red (❌):** Not found, error, missing data
- **Grey:** Ignored/excluded (delivery charges, TCS)

### Loading & Progress
- PDF parsing: show a progress bar or spinner with status text ("Uploading...", "Parsing...", "Matching items...")
- Food cost calculation: show skeleton loaders for KPI cards while computing

### Validation & Error Handling
- Prevent saving a bill with unmapped items — show count of unmapped items and disable save
- Prevent food cost calc if no base prices set — show warning with link to Item Pricing section
- Date validation: closing update must be after opening update

### Responsive Considerations
- Tables should be horizontally scrollable on smaller screens
- Mapping confirmation can be a slide-over panel on desktop, full-screen modal on smaller viewports

### Empty States
- No bills yet: "No bills uploaded yet. Upload a Hyperpure challan or enter a bill manually to get started."
- No base prices: "Set base prices for your inventory items to enable inventory valuation and food cost calculation."
- No inventory updates: "No inventory snapshots found for this branch. Stock must be updated from the mobile app first."

### Toast / Notification Patterns
- Bill saved successfully: "Bill saved — 9 items mapped to inventory"
- Mapping saved: "Item mapping saved globally"
- New item created: "New item 'Nacho Chips' created and mapped"
- Food cost calculated: No toast needed, results render in-place

---

## 5. Data Flow Diagrams

### Hyperpure Bill Upload Flow
```
User uploads PDF
       │
       ▼
POST /bills/parse-hyperpure
  → pdfplumber extracts table rows
  → For each row:
      1. Check bill_item_mappings (exact description match)
      2. If not found → fuzzy match against items collection
      3. If not found → flag as unmapped
  → Return: { billMeta, lineItems[], mappingSuggestions[] }
       │
       ▼
UI shows Review & Map screen
  → User confirms/changes mappings
  → User enters conversions where needed
  → User can create new items
       │
       ▼
POST /bills/confirm
  → Store PDF in S3
  → Save bill document in bills collection
  → Upsert confirmed mappings in bill_item_mappings
  → Return: { billId, s3Key }
```

### Food Cost Calculation Flow
```
User picks Opening Update + Closing Update
       │
       ▼
GET /food-cost?branchId=X&openingUpdateId=A&closingUpdateId=B
       │
       ▼
Backend:
  1. Fetch opening snapshot (inventory_updates doc A)
     → Get qty per item at that point
  2. Fetch closing snapshot (inventory_updates doc B)
     → Get qty per item at that point
  3. Fetch all bills WHERE branchId=X
     AND billDate BETWEEN opening.submittedAt AND closing.submittedAt
  4. For each inventory item:
     a. Collect bill line items mapped to this item
     b. If bills exist → compute weighted avg price per base unit
     c. If no bills → use item.basePricePerUnit
     d. opening_value = opening_qty_base × effective_price
     e. closing_value = closing_qty_base × effective_price
     f. purchase_value = sum of bill line item totals for this item
  5. Food Cost = Σ opening_value + Σ purchase_value - Σ closing_value
       │
       ▼
Return: { summary: {opening, purchases, closing, foodCost},
          items: [...breakdown], bills: [...used] }
```

---

## 6. New Collections Schema (Detailed)

### suppliers
```javascript
{
  _id: ObjectId,
  name: "Zomato Hyperpure",         // display name
  type: "hyperpure" | "generic",    // source type
  gstin: "06AAACZ8867B1Z4",         // optional
  isActive: true,
  createdAt: Date,
  updatedAt: Date
}
// Indexes: name (unique), type
```

### bills
```javascript
{
  _id: ObjectId,
  branchId: ObjectId,
  branchCode: "GGN01",
  supplierId: ObjectId,
  source: "hyperpure" | "generic",

  // Bill identifiers
  orderNo: "ZHPHR27-OR-0028250710",  // from challan, or user-entered
  billNumber: "MANDI-001",            // optional
  billDate: Date,                      // invoice/bill date
  orderDate: Date,                     // optional, from challan

  // File storage
  s3Key: "bills/GGN01/2026-07/abc123.pdf",
  s3Bucket: "inventory-bills",

  // Financials (bill-level totals)
  subtotal: 3880.18,        // pre-tax, pre-discount
  totalDiscount: 505.18,
  totalTaxableAmount: 3375,
  totalTaxAmount: 68.47,
  grandTotal: 3443.47,

  // Line items
  lineItems: [
    {
      slNo: 1,
      description: "Paras - Makhani Dairy Paneer, 1 Kg",
      hsnCode: "04061000",
      quantity: 2,
      unitPrice: 308,
      uom: "Pack",                    // as on bill
      preTaxTotal: 616,
      discount: 0,
      taxableAmount: 616,
      taxRate: { cgst: 0, sgst: 0, igst: 0, cess: 0 },
      taxAmount: 0,
      total: 616,

      // Mapping (filled after user confirmation)
      inventoryItemId: ObjectId,       // mapped inventory item
      inventoryItemName: "Paneer",     // denormalized for display
      inventoryItemSku: "DAI-PANEER",
      conversionFactor: 1,             // 1 Pack = 1 kg = 1000 gms
      conversionFromUnit: "Pack",
      conversionToUnit: "gms",
      quantityInBaseUnit: 2000,        // 2 packs × 1kg/pack × 1000 gms/kg
      pricePerBaseUnit: 0.308,         // ₹308 / 1000 gms
      mappingSource: "global" | "manual" | "new"  // how mapping was resolved
    }
  ],

  // Metadata
  paymentStatus: "unpaid" | "paid",
  status: "confirmed",               // always confirmed (saved after Step 2)
  createdBy: ObjectId,
  createdAt: Date,
  updatedAt: Date
}
// Indexes:
//   (branchId, billDate)  — for food cost date range queries
//   (supplierId, billDate) — for supplier history
//   (branchId, supplierId, billDate) — compound for filtered queries
```

### bill_item_mappings (global)
```javascript
{
  _id: ObjectId,
  billItemDescription: "Paras - Makhani Dairy Paneer, 1 Kg",
  hsnCode: "04061000",                // optional secondary key
  inventoryItemId: ObjectId,
  inventoryItemName: "Paneer",        // denormalized
  inventoryItemSku: "DAI-PANEER",     // denormalized

  // Conversion (if UoM differs)
  conversionFactor: 1000,             // 1 Pack = 1000 gms
  conversionFromUnit: "Pack",
  conversionToUnit: "gms",            // inventory base unit

  confirmedBy: ObjectId,              // user who confirmed
  confirmCount: 5,                    // how many times this mapping was used
  createdAt: Date,
  updatedAt: Date
}
// Indexes:
//   billItemDescription (unique) — primary lookup
//   hsnCode — secondary lookup
//   inventoryItemId — reverse lookup
```

### items collection — new field
```javascript
{
  // ... existing fields ...
  basePricePerUnit: 0.308,  // price per BASE unit (e.g., per gram)
                             // null if not set
  basePriceUpdatedAt: Date
}
```

---

## 7. API Endpoints (Detailed)

### Bills

```
POST /bills/parse-hyperpure
  Body: multipart/form-data { file: PDF }
  Returns: {
    billMeta: { orderNo, invoiceDate, orderDate, supplier, paymentStatus },
    lineItems: [
      {
        slNo, description, hsnCode, quantity, unitPrice, uom,
        preTaxTotal, discount, taxableAmount, taxRate, taxAmount, total,
        suggestedMapping: {
          inventoryItemId, inventoryItemName, confidence: "high"|"medium"|"none",
          source: "exact"|"fuzzy"|"hsn"|"none",
          existingConversion: { factor, fromUnit, toUnit } | null
        }
      }
    ],
    ignoredLines: [ { description: "Delivery Charge", total: 57.82 } ],
    totals: { subtotal, totalDiscount, taxableAmount, taxAmount, grandTotal }
  }

POST /bills/confirm
  Body: {
    branchCode: "GGN01",
    source: "hyperpure" | "generic",
    supplierId: ObjectId | null,        // null → create from supplierName
    supplierName: "New Vendor",         // used if supplierId is null
    orderNo, billNumber, billDate, orderDate, paymentStatus,
    lineItems: [
      {
        slNo, description, hsnCode, quantity, unitPrice, uom,
        preTaxTotal, discount, taxableAmount, taxRate, taxAmount, total,
        inventoryItemId,
        conversionFactor, conversionFromUnit, conversionToUnit,
        saveGlobalMapping: true | false  // whether to update bill_item_mappings
      }
    ],
    grandTotal,
    s3Upload: { fileName, contentType }  // for presigned upload URL generation
  }
  Returns: { billId, s3PresignedUploadUrl }
  Note: Frontend uploads the PDF directly to S3 using the presigned URL after this call succeeds.

GET /bills?branchCode=GGN01&dateFrom=2026-07-01&dateTo=2026-07-22&supplierId=X&source=hyperpure
  Returns: { bills: [...], totalCount }

GET /bills/:billId
  Returns: { bill, s3DownloadUrl }
  Falls back to `deleted_bills` if not found in `bills`, so archived bills stay viewable.

GET /bills/:billId/download-url
  Returns: { url: "https://s3...presigned" }
  Same archive fallback — the S3 file is never removed on delete.

DELETE /bills/:billId                                        [admin only]
  Moves the bill document from `bills` into `deleted_bills`, stamping
  status: "deleted", deletedAt and deletedBy. The S3 file is left in place.
  Returns: { message, billId }
  Note: the bill disappears from GET /bills and from food cost automatically,
  since food cost queries `bills` with status: "confirmed". Deletion is final —
  there is no restore endpoint; the archive is for audit only.

GET /bills/deleted?branchCode=GGN01&dateFrom=...&dateTo=...  [admin only]
  Same filters and response shape as GET /bills, sorted by deletedAt desc,
  with deletedAt and deletedByName added per row.
  Returns: { bills: [...], totalCount }
```

### Suppliers

```
GET /suppliers
  Returns: { suppliers: [...] }

POST /suppliers
  Body: { name, type, gstin? }
  Returns: { supplier }
```

### Item Pricing

```
PATCH /items/:itemId/base-price
  Body: { price: 308, unit: "kg" }
  → Backend converts to base unit: 308/1000 = 0.308 per gm
  Returns: { itemId, basePricePerUnit, basePriceUpdatedAt }

GET /items/pricing?branchCode=GGN01
  Returns: { items: [{ _id, sku, name, category, defaultUnit, baseUnit, basePricePerUnit, basePriceUpdatedAt }] }
```

### Bill Mappings

```
GET /bill-mappings?description=Paneer&hsnCode=04061000
  → Exact match on description first, then fuzzy
  Returns: { mappings: [...] }

POST /bill-mappings
  Body: { billItemDescription, hsnCode?, inventoryItemId, conversionFactor?, conversionFromUnit?, conversionToUnit? }
  Returns: { mapping }
```

### Food Cost

```
GET /food-cost?branchCode=GGN01&openingUpdateId=ABC&closingUpdateId=DEF
  Returns: {
    summary: {
      openingValue: 42500,
      purchaseValue: 8640,
      closingValue: 38200,
      foodCostValue: 12940,
      openingUpdate: { _id, submittedAt, itemCount },
      closingUpdate: { _id, submittedAt, itemCount },
      billCount: 3,
      period: { from: Date, to: Date }
    },
    items: [
      {
        itemId, sku, name, category,
        openingQty, openingQtyBase, openingUnit,
        purchasedQtyBase, purchaseValue,
        closingQty, closingQtyBase, closingUnit,
        effectivePricePerBaseUnit, priceSource: "bill_avg" | "base_price",
        openingValue, closingValue, costContribution
      }
    ],
    billsUsed: [
      { billId, orderNo, billDate, supplierName, source, grandTotal, itemCount, hasFile }
    ],
    warnings: [
      { type: "no_base_price", itemId, itemName, message: "No base price set and no bills found — excluded from calculation" }
    ]
  }

GET /inventory-updates/snapshots?branchCode=GGN01&limit=50
  Returns: {
    snapshots: [
      { _id, submittedAt, branchCode, updatedBy: { username }, itemCount }
    ]
  }
  → Grouped by date in the frontend
```

---

## 8. Implementation Priority (Suggested Build Order)

### Phase 1: Foundation
1. `suppliers` collection + CRUD APIs
2. `bill_item_mappings` collection + APIs
3. `basePricePerUnit` field on items + PATCH API
4. Item Pricing UI section in console

### Phase 2: Bill Upload
5. Hyperpure PDF parser (pdfplumber)
6. `POST /bills/parse-hyperpure` API
7. S3 integration (upload, presigned URLs)
8. `POST /bills/confirm` API
9. `bills` collection with indexes
10. Console: HP upload UI (drag-drop → review & map → confirm)
11. Console: Generic bill manual entry UI
12. Console: Bill history view

### Phase 3: Food Cost
13. `GET /inventory-updates/snapshots` API
14. Food cost calculation logic (pricing resolution, weighted avg)
15. `GET /food-cost` API
16. Console: Food Cost UI (snapshot picker → results → breakdown)
17. Inline bill addition from food cost view

### Phase 4: Polish
18. Empty states, loading states, error handling
19. Warnings for missing base prices
20. Mobile-responsive adjustments
21. Performance optimization (indexes, pagination)
