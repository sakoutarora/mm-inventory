"""Migration: Add database indexes for bills, mappings, and related collections.

Run once:
    python -m scripts.migration_add_indexes

Indexes are idempotent - safe to re-run.
"""

import os
import sys

from pymongo import MongoClient, ASCENDING, DESCENDING

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGO_DB", "inventory")


def run():
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]

    print(f"Connected to {MONGO_URI}, database: {DB_NAME}")
    print()

    # ---- bills collection ----
    print("Creating indexes on 'bills'...")

    # Dedup index for Hyperpure bills: branchId + source + orderNo
    db.bills.create_index(
        [("branchId", ASCENDING), ("source", ASCENDING), ("orderNo", ASCENDING)],
        name="idx_bills_branch_source_orderno",
        sparse=True,
    )
    print("  idx_bills_branch_source_orderno (dedup for HP)")

    # Filtering by branch + billDate (used by getBills, food cost)
    db.bills.create_index(
        [("branchId", ASCENDING), ("billDate", DESCENDING)],
        name="idx_bills_branch_billdate",
    )
    print("  idx_bills_branch_billdate (listing + food cost date range)")

    # Filtering by supplierId
    db.bills.create_index(
        [("supplierId", ASCENDING)],
        name="idx_bills_supplierid",
    )
    print("  idx_bills_supplierid")

    # ---- deleted_bills collection (archive for deleted bills) ----
    print("Creating indexes on 'deleted_bills'...")

    # Listing the archive: branch + most recently deleted first
    db.deleted_bills.create_index(
        [("branchId", ASCENDING), ("deletedAt", DESCENDING)],
        name="idx_deleted_bills_branch_deletedat",
    )
    print("  idx_deleted_bills_branch_deletedat (archive listing)")

    # Date-range and supplier filters mirror the active bills listing
    db.deleted_bills.create_index(
        [("branchId", ASCENDING), ("billDate", DESCENDING)],
        name="idx_deleted_bills_branch_billdate",
    )
    print("  idx_deleted_bills_branch_billdate")

    db.deleted_bills.create_index(
        [("supplierId", ASCENDING)],
        name="idx_deleted_bills_supplierid",
    )
    print("  idx_deleted_bills_supplierid")

    # ---- bill_item_mappings collection ----
    print("Creating indexes on 'bill_item_mappings'...")

    # Unique on billItemDescription (used for upsert)
    db.bill_item_mappings.create_index(
        [("billItemDescription", ASCENDING)],
        name="idx_mappings_description",
        unique=True,
    )
    print("  idx_mappings_description (unique)")

    # Lookup by hsnCode
    db.bill_item_mappings.create_index(
        [("hsnCode", ASCENDING)],
        name="idx_mappings_hsncode",
        sparse=True,
    )
    print("  idx_mappings_hsncode")

    # Lookup by inventoryItemId
    db.bill_item_mappings.create_index(
        [("inventoryItemId", ASCENDING)],
        name="idx_mappings_itemid",
    )
    print("  idx_mappings_itemid")

    # ---- suppliers collection ----
    print("Creating indexes on 'suppliers'...")

    # Unique name (case-insensitive via collation)
    db.suppliers.create_index(
        [("name", ASCENDING)],
        name="idx_suppliers_name_ci",
        unique=True,
        collation={"locale": "en", "strength": 2},
    )
    print("  idx_suppliers_name_ci (unique, case-insensitive)")

    # ---- inventory_updates collection ----
    print("Creating indexes on 'inventory_updates'...")

    # Snapshots listing: branchId + submittedAt desc
    db.inventory_updates.create_index(
        [("branchId", ASCENDING), ("submittedAt", DESCENDING)],
        name="idx_updates_branch_submitted",
    )
    print("  idx_updates_branch_submitted (snapshot listing)")

    # ---- inventory_current collection ----
    print("Creating indexes on 'inventory_current'...")

    # Lookup by branchId + itemId
    db.inventory_current.create_index(
        [("branchId", ASCENDING), ("itemId", ASCENDING)],
        name="idx_current_branch_item",
        unique=True,
    )
    print("  idx_current_branch_item (unique)")

    # ---- items collection ----
    print("Creating indexes on 'items'...")

    # SKU uniqueness
    db.items.create_index(
        [("sku", ASCENDING)],
        name="idx_items_sku",
        unique=True,
    )
    print("  idx_items_sku (unique)")

    # Active items listing
    db.items.create_index(
        [("isActive", ASCENDING), ("name", ASCENDING)],
        name="idx_items_active_name",
    )
    print("  idx_items_active_name")

    print()
    print("All indexes created successfully.")
    client.close()


if __name__ == "__main__":
    run()
