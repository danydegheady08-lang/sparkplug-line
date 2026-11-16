"""
recall.py
=========
Lot traceability from the command line, on the SQLite file a run produced.

    python recall.py CER-0003          which customers got plugs with ceramic batch CER-0003
    python recall.py --lot LOT-0004    which material batches are in customer lot LOT-0004
    python recall.py --serial SP000123 the full history of one plug
    python recall.py --list            the batches that reached shipped plugs
"""

import argparse

from sparkplug.traceability import TraceStore


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", nargs="?", help="material batch, e.g. CER-0003, STL-0002, CEW-0001, GEW-0004")
    ap.add_argument("--db", default="sparkplug_trace.db")
    ap.add_argument("--lot")
    ap.add_argument("--serial")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    store = TraceStore(a.db)

    if a.list:
        print("Batches in shipped plugs:", ", ".join(store.batches()))
    elif a.lot:
        g = store.lot_genealogy(a.lot)
        print(f"Material in {a.lot}:")
        for col, rows in g.items():
            print(f"  {col:<14} " + ", ".join(f"{b} ({n})" for b, n in rows))
    elif a.serial:
        p = store.plug(a.serial)
        if not p:
            print("No such plug")
        for k, v in (p or {}).items():
            print(f"  {k:<16} {v}")
    elif a.batch:
        hits = store.recall(a.batch)
        total = sum(h["plugs"] for h in hits)
        print(f"Recall for {a.batch.upper()}: {total} shipped plugs, {len(hits)} customer lot(s)")
        for h in hits:
            print(f"  {h['customer']:<24} {h['order_id']}  {h['lot_id']}  {h['plugs']:4d} plugs"
                  f"  serials {h['first_serial']} .. {h['last_serial']}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
