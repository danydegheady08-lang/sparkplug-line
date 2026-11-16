"""
traceability.py
===============
Lot traceability: which parts went into which plug, and which plugs went to
which customer.

Why SQLite and not InfluxDB: InfluxDB is built for values over time ("what was
the gap at 14:02"). A recall is a relational question ("which shipped plugs
contain ceramic batch CER-0007, and which customers got them"). That needs
joins and grouping over records, which a small SQL database answers directly.
SQLite ships with Python, so it needs no extra service.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugs (
    serial TEXT PRIMARY KEY,
    order_id TEXT, lot_id TEXT, customer TEXT,
    shell_id TEXT, steel_batch TEXT, ge_coil TEXT,
    insulator_id TEXT, ceramic_batch TEXT,
    center_id TEXT, ce_coil TEXT,
    thread_mm REAL, electrode_mm REAL,
    gap_preset_mm REAL, gap_mm REAL, gap_measured_mm REAL,
    status TEXT, reason TEXT, station TEXT,
    t_assembled REAL, t_done REAL
);
CREATE TABLE IF NOT EXISTS rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    t REAL, station TEXT, reason TEXT, item_id TEXT,
    order_id TEXT, scrap_eur REAL, detail TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, lot_id TEXT, customer TEXT,
    qty INTEGER, price REAL, shipped INTEGER, scrap_eur REAL,
    margin_eur REAL, t_start REAL, t_done REAL
);
CREATE TABLE IF NOT EXISTS work_orders (
    wo_id TEXT PRIMARY KEY, station TEXT, kind TEXT, reason TEXT,
    wear_pct REAL, t_created REAL, t_start REAL, t_done REAL
);
CREATE INDEX IF NOT EXISTS ix_plugs_ceramic ON plugs(ceramic_batch);
CREATE INDEX IF NOT EXISTS ix_plugs_steel ON plugs(steel_batch);
CREATE INDEX IF NOT EXISTS ix_plugs_ce ON plugs(ce_coil);
CREATE INDEX IF NOT EXISTS ix_plugs_ge ON plugs(ge_coil);
CREATE INDEX IF NOT EXISTS ix_plugs_lot ON plugs(lot_id);
"""

BATCH_COLUMNS = {"STL": "steel_batch", "CER": "ceramic_batch", "CEW": "ce_coil", "GEW": "ge_coil"}


class TraceStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._pending = 0

    # -- writing ----------------------------------------------------------------

    def plug_assembled(self, plug, order, t):
        s, i, c = plug["shell"], plug["insulator"], plug["center"]
        self.db.execute(
            "INSERT INTO plugs VALUES (?,?,?,?, ?,?,?, ?,?, ?,?, ?,?, ?,?,?, ?,?,?, ?,?)",
            (plug["serial"], order.order_id, order.lot_id, order.customer,
             s["id"], s["steel_batch"], s["ge_coil"],
             i["id"], i["ceramic_batch"],
             c["id"], c["ce_coil"],
             s.get("thread_mm"), c.get("electrode_mm"),
             s.get("gap_preset_mm"), None, None,
             "in_process", None, None, t, None))
        self._tick()

    def plug_finished(self, plug, status, t, reason=None, station=None):
        self.db.execute(
            "UPDATE plugs SET status=?, reason=?, station=?, gap_mm=?, gap_measured_mm=?, t_done=? WHERE serial=?",
            (status, reason, station, plug.get("gap_mm"), plug.get("gap_measured_mm"), t, plug["serial"]))
        self._tick()

    def reject(self, t, station, reason, item_id, order_id, scrap_eur, detail=""):
        self.db.execute(
            "INSERT INTO rejects (t, station, reason, item_id, order_id, scrap_eur, detail) VALUES (?,?,?,?,?,?,?)",
            (t, station, reason, item_id, order_id, scrap_eur, detail))
        self._tick()

    def order_done(self, o, t):
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
            (o.order_id, o.lot_id, o.customer, o.qty, o.price, o.shipped,
             round(o.scrap_eur, 2), round(o.margin_eur, 2), o.t_start, t))
        self.commit()

    def work_order(self, wo):
        self.db.execute(
            "INSERT OR REPLACE INTO work_orders VALUES (?,?,?,?,?,?,?,?)",
            (wo.wo_id, wo.station, wo.kind, wo.reason, round(wo.wear_pct, 1),
             wo.t_created, wo.t_start, wo.t_done))
        self._tick()

    def _tick(self):
        self._pending += 1
        if self._pending >= 500:
            self.commit()

    def commit(self):
        self.db.commit()
        self._pending = 0

    # -- queries ------------------------------------------------------------------

    def recall(self, batch_id):
        """Every SHIPPED plug that contains material from batch_id, grouped by
        customer and lot. This is the list a recall letter goes to."""
        col = BATCH_COLUMNS.get(batch_id[:3].upper())
        if col is None:
            raise ValueError(f"unknown batch type: {batch_id!r} (use STL-, CER-, CEW- or GEW-)")
        self.commit()
        rows = self.db.execute(
            f"""SELECT customer, order_id, lot_id, COUNT(*), MIN(serial), MAX(serial)
                FROM plugs WHERE {col} = ? AND status = 'shipped'
                GROUP BY customer, order_id, lot_id ORDER BY order_id""",
            (batch_id.upper(),)).fetchall()
        return [dict(customer=r[0], order_id=r[1], lot_id=r[2], plugs=r[3],
                     first_serial=r[4], last_serial=r[5]) for r in rows]

    def lot_genealogy(self, lot_id):
        """Which material batches went into a customer lot, and how many plugs each."""
        self.commit()
        out = {}
        for col in BATCH_COLUMNS.values():
            out[col] = self.db.execute(
                f"SELECT {col}, COUNT(*) FROM plugs WHERE lot_id=? AND status='shipped' GROUP BY {col} ORDER BY {col}",
                (lot_id,)).fetchall()
        return out

    def plug(self, serial):
        self.commit()
        cur = self.db.execute("SELECT * FROM plugs WHERE serial=?", (serial,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def reject_summary(self):
        self.commit()
        return self.db.execute(
            "SELECT station, reason, COUNT(*), ROUND(SUM(scrap_eur),2) FROM rejects GROUP BY station, reason ORDER BY 3 DESC").fetchall()

    def batches(self):
        """All material batches that reached a shipped plug (for the HMI dropdown)."""
        self.commit()
        out = []
        for col in BATCH_COLUMNS.values():
            out += [r[0] for r in self.db.execute(
                f"SELECT DISTINCT {col} FROM plugs WHERE status='shipped' ORDER BY {col}")]
        return out
