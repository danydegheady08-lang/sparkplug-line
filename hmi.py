"""
hmi.py
======
Operator screen (HMI) for the spark plug line, built with Tkinter.

    python hmi.py                 run the line in a window
    python hmi.py --influx        ... and send every second to InfluxDB for Grafana
    python hmi.py --speed 10      start at 10 x real time

Design: the screen follows the idea of a "high performance HMI" (ISA-101).
The normal state is calm grey; colour is used only where the operator must act
or should notice something: red for a fault, amber for a warning or a blocked
station, blue for planned maintenance. A screen that is colourful all the time
hides the one red box that matters.

The HMI never changes the simulation directly. It calls the operator commands
(start, stop, reset, inject_fault) and draws what snapshot() returns.
"""

import argparse
import time
import tkinter as tk
from tkinter import ttk

from sparkplug import PREDICTIVE, REACTIVE, ProductionLine
from sparkplug import config as C
from sparkplug.line import FAULTED, MAINTENANCE, REPAIRING, RUNNING, hms

# ---- colours (ISA-101 style: grey normal, colour only for abnormal) -------------------------
BG = "#D9D9D6"
PANEL = "#EDEDEB"
BOX = "#F7F7F5"
INK = "#1F2329"
MUTED = "#6B7078"
LINE = "#9AA0A6"
OK = "#2E7D32"
WARN = "#E0A100"
FAULT = "#C62828"
SERVICE = "#1E5AA8"
STATUS_COLOR = {"Working": OK, "Starved": "#9AA0A6", "Blocked": WARN, "Stopped": "#9AA0A6",
                "Fault": FAULT, "Service": SERVICE}
LEVEL_COLOR = {"info": MUTED, "warn": "#9A6B00", "reject": "#B85C00", "fault": FAULT}
SPEEDS = ["1x", "2x", "5x", "10x", "30x", "60x", "300x"]

# station and bin positions on the canvas: (x, y) of the top-left corner
BW, BH = 128, 104            # station box
BINW, BINH = 28, 74
COL = {"src": 12, "bin1": 150, "ground": 188, "bin2": 326, "asm": 372, "bin3": 510,
       "qc": 548, "bin4": 686, "pack": 724, "cust": 862}
ROW = [18, 146, 274]
STATION_POS = {"shell": (COL["src"], ROW[0]), "insulator": (COL["src"], ROW[1]),
               "center": (COL["src"], ROW[2]), "ground": (COL["ground"], ROW[0]),
               "assembly": (COL["asm"], ROW[1]), "final_qc": (COL["qc"], ROW[1]),
               "packaging": (COL["pack"], ROW[1])}
BIN_POS = {"shell": (COL["bin1"], ROW[0]), "insulator": (COL["bin1"], ROW[1]),
           "center": (COL["bin1"], ROW[2]), "shell_ge": (COL["bin2"], ROW[0]),
           "assembled": (COL["bin3"], ROW[1]), "tested": (COL["bin4"], ROW[1])}


class HMI:
    def __init__(self, root, line, telemetry=None, sink=None, speed="1x"):
        self.root, self.line, self.tel, self.sink = root, line, telemetry, sink
        self.speed = tk.StringVar(value=speed)
        self.strategy = tk.StringVar(value=line.cmms.strategy)
        self.inject_target = tk.StringVar(value=C.STATIONS["ground"][1])
        self._acc = 0.0
        self._last = time.time()
        self._last_flush = time.time()
        self._ts_ns = time.time_ns()
        self._log_seen = 0
        self._build()
        self._refresh()
        self.root.after(100, self._loop)

    # =====================================================================
    # layout
    # =====================================================================

    def _build(self):
        r = self.root
        r.title("Spark Plug Line SP1  |  Operator Screen")
        r.configure(bg=BG)
        r.geometry("1400x880")
        r.minsize(1280, 820)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=BOX, fieldbackground=BOX, foreground=INK,
                        rowheight=22, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, font=("Segoe UI", 9, "bold"))
        style.configure("TCombobox", fieldbackground=BOX)

        # ---- top bar -------------------------------------------------------------
        top = tk.Frame(r, bg=INK, height=64)
        top.pack(fill="x")
        tk.Label(top, text="SPARK PLUG LINE SP1", bg=INK, fg="white",
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=(16, 16), pady=12)
        self.state_lbl = tk.Label(top, text="", bg=INK, fg="white", width=31,
                                  font=("Segoe UI", 11, "bold"), padx=8, pady=6)
        self.state_lbl.pack(side="left", pady=10)
        self.clock_lbl = tk.Label(top, text="", bg=INK, fg="#C9CDD2", font=("Consolas", 13))
        self.clock_lbl.pack(side="left", padx=18)

        def btn(text, cmd, color):
            b = tk.Button(top, text=text, command=cmd, bg=color, fg="white", activebackground=color,
                          relief="flat", font=("Segoe UI", 10, "bold"), width=7, pady=6, cursor="hand2")
            b.pack(side="left", padx=4, pady=12)
            return b

        btn("START", self._start, OK)
        btn("STOP", self._stop, "#5F6368")
        btn("RESET", self._reset, "#B26A00")

        tk.Label(top, text="Speed", bg=INK, fg="#C9CDD2", font=("Segoe UI", 10)).pack(side="left", padx=(22, 4))
        sp = ttk.Combobox(top, textvariable=self.speed, values=SPEEDS, width=6, state="readonly")
        sp.pack(side="left")
        self.db_lbl = tk.Label(top, text="", bg=INK, fg="#C9CDD2", font=("Segoe UI", 9))
        self.db_lbl.pack(side="right", padx=16)

        # ---- alarm banner -------------------------------------------------------------
        self.banner = tk.Label(r, text="", anchor="w", font=("Segoe UI", 12, "bold"), padx=18, pady=9)
        self.banner.pack(fill="x")

        # ---- middle: line diagram + right panel ------------------------------------------------
        mid = tk.Frame(r, bg=BG)
        mid.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        left = tk.Frame(mid, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, width=1000, height=396, bg=PANEL, highlightthickness=1,
                                highlightbackground=LINE)
        self.canvas.pack(fill="x")

        right = tk.Frame(mid, bg=PANEL, highlightthickness=1, highlightbackground=LINE, width=340)
        right.pack(side="left", fill="y", padx=(12, 0))
        right.pack_propagate(False)
        self._build_right(right)

        # ---- bottom: event log + work orders + recall -------------------------------------------------
        bottom = tk.Frame(left, bg=BG)
        bottom.pack(fill="both", expand=True, pady=(10, 0))
        logf = tk.Frame(bottom, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        logf.pack(side="left", fill="both", expand=True)
        tk.Label(logf, text="EVENTS AND ALARMS", bg=PANEL, fg=MUTED, anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x", padx=10, pady=(8, 2))
        self.log_txt = tk.Text(logf, height=10, bg=BOX, fg=INK, relief="flat", wrap="word",
                               font=("Consolas", 9), padx=8, pady=6)
        self.log_txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for lvl, col in LEVEL_COLOR.items():
            self.log_txt.tag_configure(lvl, foreground=col)
        self.log_txt.configure(state="disabled")

        wof = tk.Frame(bottom, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        wof.pack(side="left", fill="both", padx=(10, 0))
        tk.Label(wof, text="WORK ORDERS (CMMS)", bg=PANEL, fg=MUTED, anchor="w",
                 font=("Segoe UI", 9, "bold")).pack(fill="x", padx=10, pady=(8, 2))
        cols = ("wo", "station", "kind", "status")
        self.wo_tree = ttk.Treeview(wof, columns=cols, show="headings", height=8)
        for c, w, h in zip(cols, (70, 120, 82, 82), ("WO", "Station", "Type", "Status")):
            self.wo_tree.heading(c, text=h)
            self.wo_tree.column(c, width=w, anchor="w")
        self.wo_tree.tag_configure("corrective", foreground=FAULT)
        self.wo_tree.tag_configure("predictive", foreground=SERVICE)
        self.wo_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_right(self, f):
        def head(text):
            tk.Label(f, text=text, bg=PANEL, fg=MUTED, anchor="w",
                     font=("Segoe UI", 9, "bold")).pack(fill="x", padx=14, pady=(12, 2))

        head("CURRENT ORDER")
        self.order_lbl = tk.Label(f, text="", bg=PANEL, fg=INK, anchor="w", justify="left",
                                  font=("Segoe UI", 11, "bold"))
        self.order_lbl.pack(fill="x", padx=14)
        self.order_sub = tk.Label(f, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left",
                                  font=("Segoe UI", 9))
        self.order_sub.pack(fill="x", padx=14)
        self.order_bar = tk.Canvas(f, height=14, bg=BOX, highlightthickness=0)
        self.order_bar.pack(fill="x", padx=14, pady=6)
        self.order_money = tk.Label(f, text="", bg=PANEL, fg=INK, anchor="w", justify="left",
                                    font=("Consolas", 10))
        self.order_money.pack(fill="x", padx=14)

        head("OEE SINCE START")
        self.oee_lbl = tk.Label(f, text="", bg=PANEL, fg=INK, font=("Segoe UI", 26, "bold"), anchor="w")
        self.oee_lbl.pack(fill="x", padx=14)
        self.oee_sub = tk.Label(f, text="", bg=PANEL, fg=INK, font=("Consolas", 10), anchor="w", justify="left")
        self.oee_sub.pack(fill="x", padx=14)

        head("SHIFT TOTALS")
        self.totals = tk.Label(f, text="", bg=PANEL, fg=INK, font=("Consolas", 10), anchor="w", justify="left")
        self.totals.pack(fill="x", padx=14)

        head("MAINTENANCE STRATEGY")
        rowm = tk.Frame(f, bg=PANEL)
        rowm.pack(fill="x", padx=10)
        for val in (PREDICTIVE, REACTIVE):
            tk.Radiobutton(rowm, text=val, value=val, variable=self.strategy, command=self._set_strategy,
                           bg=PANEL, fg=INK, activebackground=PANEL, font=("Segoe UI", 10)).pack(side="left")

        head("LOT TRACEABILITY (RECALL)")
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", padx=14, pady=(2, 4))
        self.batch_var = tk.StringVar(value="CER-0001")
        self.batch_box = ttk.Combobox(row, textvariable=self.batch_var, width=13,
                                      postcommand=self._fill_batches)
        self.batch_box.pack(side="left")
        tk.Button(row, text="Recall", command=self._recall, relief="flat", bg=INK, fg="white",
                  font=("Segoe UI", 9, "bold"), padx=10).pack(side="left", padx=6)
        self.recall_txt = tk.Text(f, height=5, bg=BOX, fg=INK, relief="flat", wrap="word",
                                  font=("Consolas", 9), padx=6, pady=4)
        self.recall_txt.pack(fill="x", padx=14)

        head("DEMO: INJECT A FAULT")
        row2 = tk.Frame(f, bg=PANEL)
        row2.pack(fill="x", padx=14, pady=(2, 10))
        ttk.Combobox(row2, textvariable=self.inject_target, width=17, state="readonly",
                     values=[C.STATIONS[k][1] for k in C.STATION_ORDER]).pack(side="left")
        tk.Button(row2, text="Break it", command=self._inject, relief="flat", bg=FAULT, fg="white",
                  font=("Segoe UI", 9, "bold"), padx=10).pack(side="left", padx=6)
        self.msg = tk.Label(f, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left",
                            wraplength=300, font=("Segoe UI", 9))
        self.msg.pack(fill="x", padx=14, pady=(0, 8))

    # =====================================================================
    # commands
    # =====================================================================

    def _say(self, result):
        ok, text = result
        self.msg.configure(text=text, fg=MUTED if ok else FAULT)
        self._refresh()

    def _start(self):
        self._say(self.line.start())

    def _stop(self):
        self._say(self.line.stop())

    def _reset(self):
        self._say(self.line.reset())

    def _inject(self):
        key = next(k for k in C.STATION_ORDER if C.STATIONS[k][1] == self.inject_target.get())
        self._say(self.line.inject_fault(key))

    def _set_strategy(self):
        self.line.cmms.strategy = self.strategy.get()
        self._say((True, f"Maintenance strategy: {self.strategy.get()}"))

    def _fill_batches(self):
        self.batch_box["values"] = self.line.trace.batches()

    def _recall(self):
        self.recall_txt.delete("1.0", "end")
        try:
            hits = self.line.trace.recall(self.batch_var.get().strip())
        except ValueError as e:
            self.recall_txt.insert("end", str(e))
            return
        total = sum(h["plugs"] for h in hits)
        self.recall_txt.insert("end", f"{self.batch_var.get().upper()}: {total} shipped plugs\n")
        for h in hits:
            self.recall_txt.insert("end", f"{h['lot_id']} {h['customer'][:16]:<16} {h['plugs']:4d}\n")
        if not hits:
            self.recall_txt.insert("end", "No shipped plug contains this batch (yet).")

    # =====================================================================
    # loop
    # =====================================================================

    def _loop(self):
        now = time.time()
        dt_real = now - self._last
        self._last = now
        speed = float(self.speed.get().rstrip("x"))
        self._acc += dt_real * speed
        n = int(self._acc)
        self._acc -= n
        step_ns = int(1e9 / speed)
        for _ in range(min(n, 3000)):
            self.line.tick()
            if self.tel:
                self._ts_ns = max(self._ts_ns + step_ns, time.time_ns() - 3_600_000_000_000)
                self.tel.record(self.line, self._ts_ns)
        if self.tel and now - self._last_flush > 1.0:
            try:
                self.sink.flush()
                self.db_lbl.configure(text=f"InfluxDB: {self.sink.count} points sent")
            except Exception as e:  # keep the HMI alive if the database goes away
                self.db_lbl.configure(text=f"InfluxDB error: {type(e).__name__}")
            self._last_flush = now
        self._refresh()
        self.root.after(100, self._loop)

    # =====================================================================
    # drawing
    # =====================================================================

    def _refresh(self):
        s = self.line.snapshot()
        self._top(s)
        self._draw_line(s)
        self._right(s)
        self._log()
        self._work_orders(s)

    def _top(self, s):
        color = {RUNNING: OK, FAULTED: FAULT, REPAIRING: WARN, MAINTENANCE: SERVICE}.get(s["state"], "#5F6368")
        self.state_lbl.configure(text=f"{s['state'].upper()}  |  {s['e10']}", bg=color)
        self.clock_lbl.configure(text=f"sim time {s['clock']}")
        if not self.tel:
            self.db_lbl.configure(text="DB off")

        f = s["fault"]
        if s["state"] == FAULTED:
            self.banner.configure(bg=FAULT, fg="white", text=(
                f"FAULT  {hms(f['t'])}   {f['station_name']}: {f['code']}  ({f['text']}).   "
                f"Line stopped. Press RESET to acknowledge and start repair {f['wo']}."))
        elif s["state"] == REPAIRING:
            self.banner.configure(bg=WARN, fg=INK, text=(
                f"REPAIR  {f['station_name']}: {f['code']} acknowledged, {f['wo']} in progress, "
                f"{s['repair_left'] / 60:.1f} min left. Press START when the repair is done."))
        elif s["state"] == MAINTENANCE:
            name = C.STATIONS[s["maint_station"]][1]
            self.banner.configure(bg=SERVICE, fg="white", text=(
                f"SCHEDULED MAINTENANCE  {name}: planned tool service, {s['maint_left'] / 60:.1f} min left. "
                f"The line restarts by itself."))
        elif s["state"] == RUNNING:
            self.banner.configure(bg="#DDE8DA", fg=OK, text="No active alarms. Line running.")
        else:
            self.banner.configure(bg=PANEL, fg=MUTED, text="Line stopped. Press START to produce.")

    def _draw_line(self, s):
        c = self.canvas
        c.delete("all")
        st = {x["key"]: x for x in s["stations"]}

        # conveyors (drawn first, under the boxes)
        def conv(x1, y1, x2, y2):
            c.create_line(x1, y1, x2, y2, fill=LINE, width=3, arrow="last", arrowshape=(9, 10, 4))

        cy = [y + BH / 2 for y in ROW]
        conv(COL["src"] + BW, cy[0], COL["bin1"], cy[0])
        conv(COL["bin1"] + BINW, cy[0], COL["ground"], cy[0])
        conv(COL["ground"] + BW, cy[0], COL["bin2"], cy[0])
        conv(COL["src"] + BW, cy[1], COL["bin1"], cy[1])
        conv(COL["src"] + BW, cy[2], COL["bin1"], cy[2])
        # into assembly: from shell_ge (row 0), insulator (row 1), center (row 2)
        ax = COL["asm"]
        conv(COL["bin2"] + BINW / 2, ROW[0] + BINH + 15, ax + 30, ROW[1])
        conv(COL["bin1"] + BINW, cy[1], ax, cy[1])
        c.create_line(COL["bin1"] + BINW, cy[2], ax + 30, cy[2], fill=LINE, width=3)
        conv(ax + 30, cy[2], ax + 30, ROW[1] + BH)
        conv(ax + BW, cy[1], COL["bin3"], cy[1])
        conv(COL["bin3"] + BINW, cy[1], COL["qc"], cy[1])
        conv(COL["qc"] + BW, cy[1], COL["bin4"], cy[1])
        conv(COL["bin4"] + BINW, cy[1], COL["pack"], cy[1])
        conv(COL["pack"] + BW, cy[1], COL["cust"], cy[1])

        for key, (x, y) in STATION_POS.items():
            self._station(c, x, y, st[key])
        for b, (x, y) in BIN_POS.items():
            self._bin(c, x, y + (BH - BINH) / 2, s["bins"][b])

        # customer box
        x, y = COL["cust"], ROW[1]
        o = s["order"]
        c.create_rectangle(x, y, x + 116, y + BH, fill=BOX, outline=LINE)
        c.create_text(x + 8, y + 10, anchor="nw", text="CUSTOMER", fill=MUTED, font=("Segoe UI", 8, "bold"))
        c.create_text(x + 8, y + 28, anchor="nw", text=o["customer"], fill=INK, width=100,
                      font=("Segoe UI", 9, "bold"))
        c.create_text(x + 8, y + 70, anchor="nw", text=f"{o['shipped']} / {o['qty']}", fill=INK,
                      font=("Consolas", 11, "bold"))

        # legend
        lx, ly = COL["qc"], ROW[2] + 8
        c.create_text(lx, ly, anchor="nw", text="STATION STATUS", fill=MUTED, font=("Segoe UI", 8, "bold"))
        for i, (name, col) in enumerate([("Working", OK), ("Starved / stopped", "#9AA0A6"),
                                         ("Blocked", WARN), ("Fault", FAULT), ("Service", SERVICE)]):
            yy = ly + 20 + i * 16
            c.create_rectangle(lx, yy, lx + 10, yy + 10, fill=col, outline="")
            c.create_text(lx + 16, yy + 5, anchor="w", text=name, fill=INK, font=("Segoe UI", 8))
        wx = lx + 150
        c.create_text(wx, ly, anchor="nw", text="TOOL WEAR BAR", fill=MUTED, font=("Segoe UI", 8, "bold"))
        for i, (name, col) in enumerate([(f"below {100*C.PLAN_AT_WEAR:.0f} %", "#7D838A"), (f"{100*C.PLAN_AT_WEAR:.0f} to 100 %: service planned", WARN),
                                         ("over 100 %: breakdown risk", FAULT)]):
            yy = ly + 20 + i * 16
            c.create_rectangle(wx, yy + 3, wx + 18, yy + 7, fill=col, outline="")
            c.create_text(wx + 24, yy + 5, anchor="w", text=name, fill=INK, font=("Segoe UI", 8))

    def _station(self, c, x, y, d):
        col = STATUS_COLOR.get(d["status"], LINE)
        abnormal = d["status"] in ("Fault", "Service")
        fill = col if abnormal else BOX
        ink = "white" if abnormal else INK
        sub = "white" if abnormal else MUTED
        c.create_rectangle(x, y, x + BW, y + BH, fill=fill, outline=col if not abnormal else col,
                           width=2 if d["status"] in ("Fault", "Blocked") else 1)
        c.create_rectangle(x, y, x + 6, y + BH, fill=col, outline="")
        c.create_text(x + 14, y + 8, anchor="nw", text=f"{d['no']}  {d['name']}", fill=ink,
                      font=("Segoe UI", 8, "bold"))
        c.create_text(x + 14, y + 26, anchor="nw", text=d["status"].upper(), fill=col if not abnormal else ink,
                      font=("Segoe UI", 8, "bold"))
        # cycle progress
        c.create_rectangle(x + 14, y + 44, x + BW - 10, y + 49, fill="#E3E3E0", outline="")
        if d["status"] == "Working":
            c.create_rectangle(x + 14, y + 44, x + 14 + (BW - 24) * min(1, d["progress"]), y + 49,
                               fill=OK, outline="")
        # wear
        w = d["wear"]
        wcol = FAULT if w >= 1 else WARN if w >= C.PLAN_AT_WEAR else "#7D838A"
        c.create_text(x + 14, y + 55, anchor="nw", text=f"wear {100 * w:3.0f} %", fill=sub, font=("Segoe UI", 8))
        c.create_rectangle(x + 72, y + 60, x + BW - 10, y + 64, fill="#E3E3E0", outline="")
        c.create_rectangle(x + 72, y + 60, x + 72 + (BW - 82) * min(1, w), y + 64, fill=wcol, outline="")
        c.create_text(x + 14, y + 72, anchor="nw", text=f"ok {d['good']}", fill=sub, font=("Consolas", 9))
        c.create_text(x + 14, y + 86, anchor="nw", text=f"rej {d['rejected']}",
                      fill="#FFD7D7" if abnormal else ("#B85C00" if d["rejected"] else sub),
                      font=("Consolas", 9))
        if d["key"] == "ground":
            c.create_text(x + BW - 8, y + 86, anchor="ne", text=f"{d['temperature']:.0f} °C",
                          fill=sub, font=("Consolas", 9))

    def _bin(self, c, x, y, n):
        cap = C.BIN_CAPACITY
        c.create_rectangle(x, y, x + BINW, y + BINH, fill=BOX, outline=LINE)
        h = BINH * n / cap
        col = WARN if n >= cap else ("#9AA0A6" if n == 0 else "#7D838A")
        c.create_rectangle(x + 3, y + BINH - h, x + BINW - 3, y + BINH - 1, fill=col, outline="")
        c.create_text(x + BINW / 2, y + BINH + 9, text=f"{n}/{cap}", fill=MUTED, font=("Consolas", 8))

    def _right(self, s):
        o = s["order"]
        self.order_lbl.configure(text=f"{o['id']}  {o['customer']}")
        self.order_sub.configure(text=f"{o['lot']}   {o['qty']} plugs at {o['price']:.2f} EUR")
        bar = self.order_bar
        bar.delete("all")
        w = bar.winfo_width() or 300
        bar.create_rectangle(0, 0, w * o["progress"], 14, fill="#5F6368", outline="")
        self.order_money.configure(text=(
            f"shipped   {o['shipped']:>6d} / {o['qty']}\n"
            f"scrap     {o['scrap_eur']:>9.2f} EUR\n"
            f"margin    {o['margin_eur']:>9.2f} EUR"))
        e = s["oee"]
        oee = e["oee"]
        self.oee_lbl.configure(text=f"{100 * oee:.1f} %",
                               fg=OK if oee >= 0.85 else (INK if oee >= 0.65 else FAULT))
        self.oee_sub.configure(text=(
            f"availability {100 * e['availability']:5.1f} %\n"
            f"performance  {100 * e['performance']:5.1f} %\n"
            f"quality      {100 * e['quality']:5.1f} %"))
        self.totals.configure(text=(
            f"plugs shipped  {s['shipped']:>8d}\n"
            f"parts rejected {s['rejected']:>8d}\n"
            f"orders done    {s['orders_done']:>8d}\n"
            f"scrap cost     {s['scrap_eur']:>8.2f} EUR\n"
            f"margin         {s['margin_eur']:>8.2f} EUR"))

    def _log(self):
        new = self.line.log_seq - self._log_seen
        if new <= 0:
            return
        entries = list(self.line.log)[-min(new, len(self.line.log)):]
        self._log_seen = self.line.log_seq
        self.log_txt.configure(state="normal")
        for t, lvl, text in entries:
            self.log_txt.insert("end", f"{hms(t)}  {text}\n", lvl)
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_txt.delete("1.0", f"{lines - 400}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _work_orders(self, s):
        rows = [(w["id"], C.STATIONS[w["station"]][1], w["kind"], w["status"]) for w in reversed(s["work_orders"])]
        current = [self.wo_tree.item(i, "values") for i in self.wo_tree.get_children()]
        if [tuple(r) for r in current] == [tuple(map(str, r)) for r in rows]:
            return
        self.wo_tree.delete(*self.wo_tree.get_children())
        for r in rows:
            self.wo_tree.insert("", "end", values=r, tags=(r[2],))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--influx", action="store_true", help="send data to InfluxDB")
    ap.add_argument("--speed", default="1x", choices=SPEEDS)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--strategy", choices=[PREDICTIVE, REACTIVE], default=PREDICTIVE)
    ap.add_argument("--trace", default="sparkplug_trace_hmi.db")
    ap.add_argument("--fast-forward", type=float, default=0, metavar="HOURS",
                    help="run this many hours before the window opens (for demos)")
    a = ap.parse_args()

    line = ProductionLine(seed=a.seed, strategy=a.strategy, trace_path=a.trace)
    for table in ("plugs", "rejects", "orders", "work_orders"):
        line.trace.db.execute(f"DELETE FROM {table}")
    if a.fast_forward:
        line.auto_operator = True
        line.start()
        line.run(a.fast_forward * 3600)
        line.auto_operator = False
        line.drain_events()

    tel = sink = None
    if a.influx:
        from sparkplug.telemetry import InfluxSink, Telemetry
        try:
            sink = InfluxSink()
            tel = Telemetry(sink)
        except Exception as e:
            print(f"InfluxDB not reachable ({e}). Running without the database.")
    root = tk.Tk()
    HMI(root, line, tel, sink, a.speed)
    root.mainloop()
    if sink:
        sink.close()
    line.trace.commit()


if __name__ == "__main__":
    main()
