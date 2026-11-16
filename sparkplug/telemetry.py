"""
telemetry.py
============
Sends the line's data to InfluxDB (or to a file for a dry run).

Every simulated second produces one point for the line and one per station.
Events (a reject, a gap measurement, a work order, a finished order, an alarm)
produce one extra point each. Points are written as InfluxDB line protocol:

    measurement,tag=value,tag=value field=value,field=value timestamp

Tags say where a value comes from (line, station, reason) and are indexed,
so Grafana can filter and group by them. Fields hold the values.

Measurements written:
    line       state, parts, OEE, scrap and margin of the whole line
    station    status, tool wear, bin fill, good/rejected, weld temperature
    reject     one point per rejected part, tagged with station and reason
    gap        one point per final QC measurement, tagged pass / fail
    workorder  one point per finished work order (predictive or corrective)
    order      one point per finished customer order, with its margin
    alarm      one point per breakdown
"""

import os

from . import config as C

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "sparkplug-token")
INFLUX_ORG = os.getenv("INFLUX_ORG", "srh")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sparkplug")


# ---------------------------------------------------------------------------
# line protocol helpers
# ---------------------------------------------------------------------------

def _tag(v):
    return str(v).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _field(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(round(v, 6))
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def point(measurement, tags, fields, ts_ns):
    tag_s = ",".join(f"{_tag(k)}={_tag(v)}" for k, v in tags.items() if v is not None)
    field_s = ",".join(f"{_tag(k)}={_field(v)}" for k, v in fields.items() if v is not None)
    return f"{measurement},{tag_s} {field_s} {int(ts_ns)}"


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------

class FileSink:
    """Dry run: write line protocol to a text file instead of a database."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.count = 0

    def write(self, lines):
        self.f.write("\n".join(lines) + "\n")
        self.count += len(lines)

    def close(self):
        self.f.close()


class InfluxSink:
    """Buffered writer for InfluxDB 2.x."""

    def __init__(self, url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, bucket=INFLUX_BUCKET,
                 batch=5000):
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=30_000)
        self.client.ping()  # fail early with a clear error if the database is not running
        self.api = self.client.write_api(write_options=SYNCHRONOUS)
        self.bucket, self.org, self.batch = bucket, org, batch
        self.buf = []
        self.count = 0

    def write(self, lines):
        self.buf.extend(lines)
        if len(self.buf) >= self.batch:
            self.flush()

    def flush(self):
        if self.buf:
            self.api.write(bucket=self.bucket, org=self.org, record=self.buf, write_precision="ns")
            self.count += len(self.buf)
            self.buf = []

    def close(self):
        self.flush()
        self.client.close()


# ---------------------------------------------------------------------------
# the mapping from the line to points
# ---------------------------------------------------------------------------

class Telemetry:
    def __init__(self, sink, line_id=C.LINE_ID, every_s=1):
        self.sink = sink
        self.line_id = line_id
        self.every = max(1, int(every_s))
        self._n = 0

    def record(self, line, ts_ns):
        """Call once per tick, with the timestamp that tick should get."""
        L = self.line_id
        out = []
        self._n += 1
        if self._n % self.every == 0:
            o, e = line.order, line.oee
            e10 = line.e10_state()
            out.append(point("line", {"line": L}, {
                "state": e10,
                "state_code": {"Productive": 1, "Standby": 2, "Unscheduled Downtime": 3,
                               "Scheduled Downtime": 4}[e10],
                "line_state": line.state,
                "parts_good": line.shipped_total,
                "parts_rejected": line.rejected_total,
                "scrap_eur": float(line.scrap_total),
                "margin_eur": float(line.margin_total),
                "oee": e.oee, "availability": e.availability,
                "performance": e.performance, "quality": e.quality,
                "order_id": o.order_id, "order_customer": o.customer,
                "order_progress": float(o.progress),
                "order_margin_eur": float(o.margin_eur),
                "order_scrap_eur": float(o.scrap_eur),
            }, ts_ns))
            for st in line.stations.values():
                fields = {"status": st.status, "wear_pct": 100.0 * st.wear,
                          "good": st.good, "rejected": st.rejected,
                          "scrap_eur": float(st.scrap_total)}
                if st.output:
                    fields["bin_fill"] = len(line.bins[st.output])
                if st.key == "ground":
                    fields["temperature"] = float(st.temperature)
                out.append(point("station", {"line": L, "station": st.name}, fields, ts_ns))

        for i, ev in enumerate(line.drain_events(), start=1):
            ts = ts_ns + i * 1000               # keep events in the same second apart
            typ = ev["type"]
            if typ == "reject":
                name = C.STATIONS[ev["station"]][1]
                out.append(point("reject", {"line": L, "station": name, "reason": ev["reason"]},
                                 {"count": 1, "scrap_eur": float(ev["scrap_eur"]),
                                  "item": ev["item"],
                                  "value": float(ev["value"]) if ev["value"] is not None else None},
                                 ts))
            elif typ == "gap":
                out.append(point("gap", {"line": L, "result": ev["result"]},
                                 {"gap_mm": float(ev["gap_mm"]), "serial": ev["serial"]}, ts))
            elif typ == "workorder":
                w = ev["wo"]
                out.append(point("workorder", {"line": L, "station": C.STATIONS[w.station][1],
                                               "kind": w.kind},
                                 {"wo_id": w.wo_id, "reason": w.reason,
                                  "duration_s": float(w.t_done - w.t_start),
                                  "wear_pct": float(w.wear_pct)}, ts))
            elif typ == "order":
                o = ev["order"]
                out.append(point("order", {"line": L, "order_id": o.order_id,
                                           "customer": o.customer, "lot_id": o.lot_id},
                                 {"qty": o.qty, "shipped": o.shipped,
                                  "scrap_eur": round(float(o.scrap_eur), 2),
                                  "margin_eur": round(float(o.margin_eur), 2),
                                  "margin_pct": round(float(o.margin_pct), 2),
                                  "price": float(o.price),
                                  "duration_min": round((o.t_done - o.t_start) / 60, 1)}, ts))
            elif typ == "alarm":
                out.append(point("alarm", {"line": L, "station": C.STATIONS[ev["station"]][1],
                                           "code": ev["code"]},
                                 {"count": 1, "wo_id": ev["wo"].wo_id}, ts))
        if out:
            self.sink.write(out)

    def close(self):
        self.sink.close()
