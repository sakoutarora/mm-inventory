#!/usr/bin/env python3
"""Backfill inventory_snapshots collection from inventory_updates history.

Reconstructs a full branch snapshot for each inventory update by starting from
the current state (inventory_current) and walking backwards through updates,
undoing each change to reconstruct historical state.

Usage:
  python3 migration/05_backfill_inventory_snapshots.py --dry-run
  python3 migration/05_backfill_inventory_snapshots.py
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from src.lib.config import MONGODB_DB, MONGODB_URI


def run(dry_run: bool = False):
    client = MongoClient(MONGODB_URI)
    db = client[MONGODB_DB]

    try:
        # Get all active branches
        branches = list(db.branches.find({"isActive": True}))
        print(f"Found {len(branches)} active branch(es)")

        # Load all active items for sku/name/categoryId lookups
        item_docs = {str(d["_id"]): d for d in db.items.find({"isActive": True})}
        print(f"Loaded {len(item_docs)} active items")

        total_snapshots = 0

        for branch in branches:
            branch_id = branch["_id"]
            branch_code = branch.get("code", "?")
            print(f"\n--- Branch: {branch_code} (id={branch_id}) ---")

            # 1. Build current snapshot from inventory_current
            current_rows = list(db.inventory_current.find({"branchId": branch_id}))
            print(f"  inventory_current: {len(current_rows)} items")

            current_snapshot = {}
            for cur in current_rows:
                item_id_str = str(cur["itemId"])
                item_doc = item_docs.get(item_id_str)
                if not item_doc:
                    continue
                current_snapshot[item_id_str] = {
                    "itemId": cur["itemId"],
                    "sku": item_doc.get("sku"),
                    "name": item_doc.get("name"),
                    "categoryId": item_doc.get("categoryId"),
                    "quantityBase": cur.get("quantityBase"),
                    "baseUnit": cur.get("baseUnit"),
                    "quantity": cur.get("quantity"),
                    "unit": cur.get("unit"),
                }

            # 2. Load all updates sorted by createdAt descending (newest first)
            updates = list(
                db.inventory_updates.find({"branchId": branch_id}).sort("createdAt", -1)
            )
            print(f"  inventory_updates: {len(updates)} records")

            if not updates:
                print("  No updates to backfill, skipping.")
                continue

            # 3. Walk from newest to oldest, building snapshots
            snapshots_to_insert = []
            working_snapshot = dict(current_snapshot)

            for update in updates:
                # Record snapshot for THIS update (state AFTER this update was applied)
                snapshot_doc = {
                    "updateId": update["_id"],
                    "branchId": branch_id,
                    "createdAt": update.get("createdAt") or update["submittedAt"],
                    "items": list(working_snapshot.values()),
                }
                snapshots_to_insert.append(snapshot_doc)

                # Undo this update's changes to get state BEFORE this update
                # Deep copy so mutations don't affect already-recorded snapshots
                working_snapshot = {k: dict(v) for k, v in working_snapshot.items()}

                for item in update.get("items", []):
                    item_id_str = str(item["itemId"])
                    prev_qty_base = item.get("previousQuantityBase")

                    if prev_qty_base is None:
                        # First appearance of this item - remove from snapshot
                        working_snapshot.pop(item_id_str, None)
                    else:
                        # Revert to previous quantity
                        if item_id_str in working_snapshot:
                            working_snapshot[item_id_str] = dict(working_snapshot[item_id_str])
                            working_snapshot[item_id_str]["quantityBase"] = prev_qty_base
                            working_snapshot[item_id_str]["quantity"] = prev_qty_base

            # 4. Verification: most recent snapshot should match inventory_current
            most_recent = snapshots_to_insert[0]
            snapshot_by_item = {
                str(s["itemId"]): s["quantityBase"] for s in most_recent["items"]
            }
            current_by_item = {
                str(c["itemId"]): c.get("quantityBase") for c in current_rows
                if item_docs.get(str(c["itemId"]))  # only active items
            }

            mismatches = []
            for item_id, qty in current_by_item.items():
                snap_qty = snapshot_by_item.get(item_id)
                if snap_qty != qty:
                    name = item_docs.get(item_id, {}).get("name", "?")
                    mismatches.append(f"    {name}: current={qty}, snapshot={snap_qty}")

            if mismatches:
                print(f"  WARNING: {len(mismatches)} mismatch(es) with inventory_current:")
                for m in mismatches:
                    print(m)
            else:
                print(f"  Verification OK: most recent snapshot matches inventory_current")

            # 5. Insert
            if not dry_run:
                deleted = db.inventory_snapshots.delete_many({"branchId": branch_id})
                print(f"  Deleted {deleted.deleted_count} existing snapshots")

                if snapshots_to_insert:
                    db.inventory_snapshots.insert_many(snapshots_to_insert)

                # Create indexes
                db.inventory_snapshots.create_index("updateId", unique=True)
                db.inventory_snapshots.create_index([("branchId", 1), ("createdAt", -1)])

            total_snapshots += len(snapshots_to_insert)
            print(f"  {'[DRY RUN] Would insert' if dry_run else 'Inserted'} {len(snapshots_to_insert)} snapshots")

        print(f"\nTotal: {total_snapshots} snapshots {'(dry run)' if dry_run else 'inserted'}")

    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill inventory_snapshots collection.")
    parser.add_argument("--dry-run", action="store_true", help="Verify only; do not write.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
