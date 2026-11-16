"""
build_dashboard.py
==================
Generates grafana/dashboards/sparkplug.json, the dashboard Grafana loads at
start-up. Writing it from Python keeps every Flux query readable in one place
and avoids hand-editing a long JSON file.

    python build_dashboard.py
"""

import json
from pathlib import Path

DS = {"type": "influxdb", "uid": "influx-sparkplug"}
B = 'from(bucket: "sparkplug")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
STATION_FILTER = 'r.station =~ /^${station:regex}$/'

STATE_COLORS = {
    "Productive": "green", "Standby": "yellow",
    "Unscheduled Downtime": "red", "Scheduled Downtime": "blue",
    "Working": "green", "Starved": "yellow", "Blocked": "orange",
    "Stopped": "#808080", "Fault": "red", "Service": "blue",
}

panels = []
_id = 0


def add(ptype, title, x, y, w, h, query, **extra):
    global _id
    _id += 1
    p = {
        "id": _id, "type": ptype, "title": title, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "datasource": DS, "query": query}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    for k, v in extra.items():
        if k in ("defaults", "overrides"):
            p["fieldConfig"][k].update(v) if k == "defaults" else p["fieldConfig"][k].extend(v)
        else:
            p[k] = v
    panels.append(p)
    return p


def stat(title, x, y, w, field, unit=None, decimals=None, thresholds=None, color="fixed", desc=""):
    defaults = {"color": {"mode": "thresholds" if thresholds else "fixed", "fixedColor": "#C9A96E"}}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return add("stat", title, x, y, w, 4,
               B + f'  |> filter(fn: (r) => r._measurement == "line" and r._field == "{field}")\n'
                   '  |> last()',
               defaults=defaults, description=desc,
               options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "value",
                        "justifyMode": "center", "orientation": "auto"})


def mapping(colors):
    return [{"type": "value", "options": {k: {"color": v, "index": i}
                                          for i, (k, v) in enumerate(colors.items())}}]


# ---- row 1: the numbers a plant manager asks for --------------------------------------------
pct = [{"color": "red", "value": None}, {"color": "orange", "value": 0.65}, {"color": "green", "value": 0.85}]
stat("OEE", 0, 0, 4, "oee", "percentunit", 1, pct, desc="Availability x Performance x Quality since the start of the run")
stat("Availability", 4, 0, 3, "availability", "percentunit", 1, pct)
stat("Performance", 7, 0, 3, "performance", "percentunit", 1, pct)
stat("Quality", 10, 0, 3, "quality", "percentunit", 1, pct)
stat("Plugs shipped", 13, 0, 3, "parts_good", "none", 0)
stat("Scrap cost", 16, 0, 4, "scrap_eur", "prefix:€", 2, desc="Value lost in rejected parts and plugs")
stat("Margin", 20, 0, 4, "margin_eur", "prefix:€", 0, desc="price x shipped - (cost of shipped plugs + scrap)")

# ---- row 2: machine state ---------------------------------------------------------------------------
add("state-timeline", "Line state (E10)", 0, 4, 18, 5,
    B + '  |> filter(fn: (r) => r._measurement == "line" and r._field == "state")\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        '  |> rename(columns: {_value: "Line SP1"})',
    defaults={"mappings": mapping({k: STATE_COLORS[k] for k in
                                   ["Productive", "Standby", "Unscheduled Downtime", "Scheduled Downtime"]}),
              "thresholds": {"mode": "absolute", "steps": [{"color": "#808080", "value": None}]},
              "color": {"mode": "thresholds"}},
    options={"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
             "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}})

add("gauge", "Current order", 18, 4, 6, 5,
    B + '  |> filter(fn: (r) => r._measurement == "line" and r._field == "order_progress")\n'
        '  |> last()',
    defaults={"unit": "percentunit", "min": 0, "max": 1, "decimals": 0,
              "color": {"mode": "fixed", "fixedColor": "#C9A96E"},
              "thresholds": {"mode": "absolute", "steps": [{"color": "#C9A96E", "value": None}]}},
    options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
             "showThresholdMarkers": False})

# ---- row 3: quality -----------------------------------------------------------------------------------------
add("timeseries", "Electrode gap at final QC (limits 0.75 / 0.85 mm)", 0, 9, 14, 8,
    B + '  |> filter(fn: (r) => r._measurement == "gap" and r._field == "gap_mm")\n'
        '  |> keep(columns: ["_time", "_value", "result"])',
    defaults={"unit": "suffix: mm", "decimals": 2, "min": 0.6, "max": 1.0,
              "custom": {"drawStyle": "points", "pointSize": 3, "showPoints": "always",
                         "thresholdsStyle": {"mode": "dashed"}},
              "thresholds": {"mode": "absolute", "steps": [
                  {"color": "red", "value": None}, {"color": "green", "value": 0.75},
                  {"color": "red", "value": 0.85}]},
              "color": {"mode": "fixed", "fixedColor": "#5B8DEF"}},
    overrides=[{"matcher": {"id": "byFrameRefID", "options": "A"},
                "properties": [{"id": "displayName", "value": "${__field.labels.result}"}]},
               {"matcher": {"id": "byName", "options": "fail"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
               {"matcher": {"id": "byName", "options": "pass"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#5B8DEF"}}]}],
    options={"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
             "tooltip": {"mode": "single"}})

add("barchart", "Why plugs are rejected (count by reason)", 14, 9, 10, 8,
    B + '  |> filter(fn: (r) => r._measurement == "reject" and r._field == "count")\n'
        '  |> group(columns: ["reason"])\n'
        '  |> sum()\n'
        '  |> group()\n'
        '  |> keep(columns: ["reason", "_value"])\n'
        '  |> rename(columns: {_value: "Rejects"})\n'
        '  |> sort(columns: ["Rejects"], desc: true)',
    defaults={"color": {"mode": "fixed", "fixedColor": "#C9A96E"}},
    overrides=[{"matcher": {"id": "byName", "options": "Rejects"},
                "properties": [{"id": "custom.axisPlacement", "value": "hidden"}]}],
    options={"orientation": "horizontal", "xField": "reason", "showValue": "always",
             "legend": {"showLegend": False}, "barWidth": 0.7})

# ---- row 4: money ---------------------------------------------------------------------------------------------
add("barchart", "Scrap cost by station", 0, 17, 8, 8,
    B + '  |> filter(fn: (r) => r._measurement == "reject" and r._field == "scrap_eur")\n'
        '  |> group(columns: ["station"])\n'
        '  |> sum()\n'
        '  |> group()\n'
        '  |> keep(columns: ["station", "_value"])\n'
        '  |> rename(columns: {_value: "Scrap EUR"})\n'
        '  |> sort(columns: ["Scrap EUR"], desc: true)',
    defaults={"unit": "prefix:€", "decimals": 2, "color": {"mode": "fixed", "fixedColor": "#B5594B"},
              },
    overrides=[{"matcher": {"id": "byName", "options": "Scrap EUR"},
                "properties": [{"id": "custom.axisPlacement", "value": "hidden"}]}],
    options={"orientation": "horizontal", "xField": "station", "showValue": "always",
             "legend": {"showLegend": False}, "barWidth": 0.7})

add("table", "Finished customer orders", 8, 17, 16, 8,
    B + '  |> filter(fn: (r) => r._measurement == "order")\n'
        '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> group()\n'
        '  |> keep(columns: ["_time", "order_id", "lot_id", "customer", "qty", "price", "scrap_eur", "margin_eur", "margin_pct", "duration_min"])\n'
        '  |> sort(columns: ["_time"], desc: true)',
    overrides=[
        {"matcher": {"id": "byName", "options": "_time"}, "properties": [{"id": "displayName", "value": "Finished"}]},
        {"matcher": {"id": "byName", "options": "order_id"}, "properties": [{"id": "displayName", "value": "Order"}]},
        {"matcher": {"id": "byName", "options": "lot_id"}, "properties": [{"id": "displayName", "value": "Lot"}]},
        {"matcher": {"id": "byName", "options": "customer"}, "properties": [{"id": "displayName", "value": "Customer"}]},
        {"matcher": {"id": "byName", "options": "qty"}, "properties": [{"id": "displayName", "value": "Plugs"}]},
        {"matcher": {"id": "byName", "options": "price"}, "properties": [{"id": "displayName", "value": "Price"}, {"id": "unit", "value": "prefix:€"}, {"id": "decimals", "value": 2}]},
        {"matcher": {"id": "byName", "options": "scrap_eur"}, "properties": [{"id": "displayName", "value": "Scrap"}, {"id": "unit", "value": "prefix:€"}, {"id": "decimals", "value": 2}]},
        {"matcher": {"id": "byName", "options": "margin_eur"}, "properties": [{"id": "displayName", "value": "Margin"}, {"id": "unit", "value": "prefix:€"}, {"id": "decimals", "value": 2}]},
        {"matcher": {"id": "byName", "options": "margin_pct"}, "properties": [
            {"id": "displayName", "value": "Margin %"}, {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
            {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                {"color": "red", "value": None}, {"color": "orange", "value": 35}, {"color": "green", "value": 42}]}}]},
        {"matcher": {"id": "byName", "options": "duration_min"}, "properties": [{"id": "displayName", "value": "Minutes"}]},
    ],
    options={"showHeader": True, "cellHeight": "sm"})

# ---- row 5: maintenance (filtered by the station variable) --------------------------------------------------------------
add("timeseries", "Tool wear by station ($station)", 0, 25, 14, 8,
    B + f'  |> filter(fn: (r) => r._measurement == "station" and r._field == "wear_pct" and {STATION_FILTER})\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)',
    defaults={"unit": "percent", "min": 0, "max": 120, "decimals": 0,
              "displayName": "${__field.labels.station}",
              "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0,
                         "thresholdsStyle": {"mode": "dashed"}},
              "thresholds": {"mode": "absolute", "steps": [
                  {"color": "transparent", "value": None}, {"color": "orange", "value": 40},
                  {"color": "red", "value": 100}]},
              "color": {"mode": "palette-classic"}},
    description="Cycles since the last service as a share of the tool life. Orange (40 %): predictive work order opens. Red: end of life.",
    options={"legend": {"showLegend": True, "displayMode": "list", "placement": "right"},
             "tooltip": {"mode": "multi"}})

add("table", "Work orders (CMMS)", 14, 25, 10, 8,
    B + f'  |> filter(fn: (r) => r._measurement == "workorder" and {STATION_FILTER})\n'
        '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> group()\n'
        '  |> map(fn: (r) => ({r with duration_min: r.duration_s / 60.0}))\n'
        '  |> keep(columns: ["_time", "wo_id", "station", "kind", "reason", "duration_min"])\n'
        '  |> sort(columns: ["_time"], desc: true)',
    overrides=[
        {"matcher": {"id": "byName", "options": "_time"}, "properties": [{"id": "displayName", "value": "Done"}]},
        {"matcher": {"id": "byName", "options": "wo_id"}, "properties": [{"id": "displayName", "value": "WO"}]},
        {"matcher": {"id": "byName", "options": "station"}, "properties": [{"id": "displayName", "value": "Station"}]},
        {"matcher": {"id": "byName", "options": "reason"}, "properties": [{"id": "displayName", "value": "Reason"}]},
        {"matcher": {"id": "byName", "options": "duration_min"}, "properties": [
            {"id": "displayName", "value": "Minutes"}, {"id": "decimals", "value": 0}]},
        {"matcher": {"id": "byName", "options": "kind"}, "properties": [
            {"id": "displayName", "value": "Type"},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
            {"id": "mappings", "value": mapping({"predictive": "blue", "corrective": "red"})}]},
    ],
    options={"showHeader": True, "cellHeight": "sm"})

add("state-timeline", "Station status ($station)", 0, 33, 14, 8,
    B + f'  |> filter(fn: (r) => r._measurement == "station" and r._field == "status" and {STATION_FILTER})\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)',
    defaults={"displayName": "${__field.labels.station}",
              "mappings": mapping({k: STATE_COLORS[k] for k in
                                   ["Working", "Starved", "Blocked", "Stopped", "Fault", "Service"]}),
              "thresholds": {"mode": "absolute", "steps": [{"color": "#808080", "value": None}]},
              "color": {"mode": "thresholds"}},
    options={"showValue": "never", "mergeValues": True, "rowHeight": 0.85,
             "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}})

add("timeseries", "Welding station temperature", 14, 33, 10, 8,
    B + '  |> filter(fn: (r) => r._measurement == "station" and r._field == "temperature")\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        '  |> rename(columns: {_value: "Ground electrode"})',
    defaults={"unit": "celsius", "decimals": 1,
              "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10},
              "color": {"mode": "fixed", "fixedColor": "#E8833A"}},
    description="Heats up while the station welds and cools down when it stands still.",
    options={"legend": {"showLegend": False}, "tooltip": {"mode": "single"}})

add("timeseries", "Bin fill between stations ($station)", 0, 41, 24, 7,
    B + f'  |> filter(fn: (r) => r._measurement == "station" and r._field == "bin_fill" and {STATION_FILTER})\n'
        '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)',
    defaults={"min": 0, "max": 12, "decimals": 0, "displayName": "after ${__field.labels.station}",
              "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 0},
              "color": {"mode": "palette-classic"}},
    description="Parts waiting after each station (capacity 12). Full means the station before is blocked, empty means the one after is starved.",
    options={"legend": {"showLegend": True, "displayMode": "list", "placement": "right"},
             "tooltip": {"mode": "multi"}})

dashboard = {
    "uid": "sparkplug-sp1",
    "title": "Spark Plug Line SP1",
    "tags": ["sparkplug", "advanced-programming"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 3,
    "editable": True,
    "refresh": "10s",
    "time": {"from": "now-9h", "to": "now"},
    "templating": {"list": [{
        "name": "station", "label": "Station", "type": "query", "datasource": DS,
        "query": 'import "influxdata/influxdb/schema"\n'
                 'schema.tagValues(bucket: "sparkplug", tag: "station", '
                 'predicate: (r) => r._measurement == "station", start: -30d)',
        "definition": "station tag values",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "sort": 0,
    }]},
    "annotations": {"list": []},
    "panels": panels,
}

out = Path(__file__).parent / "grafana" / "dashboards" / "sparkplug.json"
out.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")
print(f"wrote {out} with {len(panels)} panels")
