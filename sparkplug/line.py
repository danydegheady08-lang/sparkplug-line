"""
line.py
=======
The back end of the spark plug line. No drawing code and no database client:
the HMI and the InfluxDB writer only read snapshot() and drain_events(), so the
same simulation runs in a window, headless, or inside the tests.

Material flow (bins of limited size between the stations):

    1 Shell ------------> [shell] --> 4 Ground electrode --> [shell_ge] --+
    2 Insulator --------> [insulator] ----------------------------------+--> 5 Assembly
    3 Center electrode -> [center] -------------------------------------+        |
                                                                                [assembled]
                                                      6 Final QC <---------------+
                                                          |
                                                      [tested] --> 7 Packaging --> customer order

Line states (what the operator commands) and their E10 meaning:

    Running      Productive while assembly works, Standby when it is starved or blocked
    Stopped      Standby (stopped by the operator)
    Faulted      Unscheduled Downtime: a station broke down, waiting for Reset
    Repairing    Unscheduled Downtime: Reset pressed, corrective repair running
    Maintenance  Scheduled Downtime: predictive work orders being carried out
"""

import random
from collections import Counter, deque

from . import config as C
from .economics import OEE, OrderBook
from .maintenance import CMMS, PREDICTIVE
from .quality import CHECKS, REASONS
from .traceability import TraceStore

STOPPED, RUNNING, FAULTED, REPAIRING, MAINTENANCE = (
    "Stopped", "Running", "Faulted", "Repairing", "Maintenance")

PRODUCTIVE, STANDBY = "Productive", "Standby"
UNSCHEDULED, SCHEDULED = "Unscheduled Downtime", "Scheduled Downtime"
E10_CODE = {PRODUCTIVE: 1, STANDBY: 2, UNSCHEDULED: 3, SCHEDULED: 4}

FAULT_TEXT = {
    "TOOL_BREAK": "Cutting tool broke while turning the shell",
    "KILN_OVERTEMP": "Sintering kiln over temperature",
    "WIRE_FEED_JAM": "Electrode wire jammed in the feeder",
    "WELD_TIP_STUCK": "Welding electrode tip stuck to the shell",
    "PRESS_OVERLOAD": "Crimping press overload",
    "GAUGE_DRIFT": "Gap gauge out of calibration",
    "LABEL_JAM": "Label printer jammed",
}

BINS = ["shell", "insulator", "center", "shell_ge", "assembled", "tested"]


def hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


class Station:
    def __init__(self, key):
        (self.no, self.name, self.cycle_s, self.life, self.inputs, self.output,
         self.scrap_eur, self.service_s, self.repair_s, self.fault_code) = C.STATIONS[key]
        self.key = key
        self.status = "Stopped"      # Working, Starved, Blocked, Stopped, Fault, Service
        self.unit = None             # unit being processed
        self.progress = 0.0
        self.held = None             # finished unit waiting for space in the output bin
        self.wear_cycles = 0
        self.cycles = 0
        self.good = 0
        self.rejected = 0
        self.scrap_total = 0.0
        self.temperature = C.AMBIENT_C

    @property
    def wear(self):
        return self.wear_cycles / self.life


class ProductionLine:
    def __init__(self, seed=7, strategy=PREDICTIVE, trace_path=":memory:", auto_operator=False):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.state = STOPPED
        self.auto_operator = auto_operator
        self.stations = {k: Station(k) for k in C.STATION_ORDER}
        self.bins = {b: deque() for b in BINS}
        self.cmms = CMMS(strategy)
        self.book = OrderBook(self.rng)
        self.order = self.book.next_order(0.0)
        self.oee = OEE(C.STATIONS[C.BOTTLENECK][2])
        self.trace = TraceStore(trace_path)
        self.state_seconds = Counter()
        self.reject_counts = Counter()        # (station, reason) -> count
        self.alarms = []                      # active and acknowledged alarms
        self.fault = None                     # the alarm that stopped the line
        self.log = deque(maxlen=300)          # (t, level, text) for the HMI
        self.log_seq = 0                      # how many entries were ever written
        self._events = []                     # drained by the telemetry writer
        self._serial = 0
        self._ids = Counter()
        self._repair_wo = None
        self._repair_left = 0.0
        self._maint_queue = []
        self._maint_wo = None
        self._maint_left = 0.0
        self._reset_at = None
        self._note("info", f"Line ready. First order {self.order.order_id}: "
                           f"{self.order.qty} plugs for {self.order.customer}")

    # ======================================================================
    # operator commands (the HMI buttons)
    # ======================================================================

    def start(self):
        if self.state == STOPPED:
            self.state = RUNNING
            self._note("info", "START pressed, line running")
            return True, "Line running"
        if self.state in (FAULTED, REPAIRING):
            return False, "Line is faulted: press RESET and wait for the repair first"
        return False, f"Start not possible while {self.state.lower()}"

    def stop(self):
        if self.state == RUNNING:
            self.state = STOPPED
            self._note("info", "STOP pressed, line stopped")
            return True, "Line stopped"
        return False, f"Stop not possible while {self.state.lower()}"

    def reset(self):
        if self.state == FAULTED:
            self.state = REPAIRING
            self._repair_left = self._repair_wo.duration_s
            self._repair_wo.t_start = self.t
            self.fault["acknowledged"] = True
            self._note("warn", f"RESET pressed: alarm acknowledged, repair {self._repair_wo.wo_id} "
                               f"started ({self._repair_left / 60:.0f} min)")
            return True, "Alarm acknowledged, repair running"
        if self.state == STOPPED:
            self.alarms = [a for a in self.alarms if not a.get("acknowledged")]
            self._note("info", "RESET pressed: acknowledged alarms cleared")
            return True, "Acknowledged alarms cleared"
        return False, f"Reset not needed while {self.state.lower()}"

    # ======================================================================
    # simulation step
    # ======================================================================

    def e10_state(self):
        if self.state in (FAULTED, REPAIRING):
            return UNSCHEDULED
        if self.state == MAINTENANCE:
            return SCHEDULED
        if self.state == RUNNING and self.stations[C.BOTTLENECK].status == "Working":
            return PRODUCTIVE
        return STANDBY

    def tick(self, dt=C.TICK_S):
        self.t += dt
        if self.state == RUNNING:
            self._run_stations(dt)
            if self.state == RUNNING:
                self._predictive_check()
        elif self.state == REPAIRING:
            self._repair_step(dt)
        elif self.state == MAINTENANCE:
            self._maintenance_step(dt)
        elif self.state == FAULTED and self.auto_operator and self.t >= self._reset_at:
            self.reset()

        if self.state != RUNNING:
            for st in self.stations.values():
                st.status = "Stopped"
            if self.fault and self.state in (FAULTED, REPAIRING):
                self.stations[self.fault["station"]].status = "Fault"
            if self._maint_wo is not None:
                self.stations[self._maint_wo.station].status = "Service"

        e10 = self.e10_state()
        self.state_seconds[e10] += dt
        self.oee.tick(dt, planned=self.state in (RUNNING, FAULTED, REPAIRING),
                      running=self.state == RUNNING)
        self._temperatures(dt)

    def run(self, seconds):
        for _ in range(int(seconds / C.TICK_S)):
            self.tick()

    # ======================================================================
    # stations
    # ======================================================================

    def _run_stations(self, dt):
        # downstream first, so a station can hand over into space freed this second
        for key in reversed(C.STATION_ORDER):
            st = self.stations[key]
            if st.held is not None:
                if self._push(st, st.held):
                    st.held = None
                else:
                    st.status = "Blocked"
                    continue
            if st.unit is None:
                if not all(self.bins[b] for b in st.inputs):
                    st.status = "Starved"
                    continue
                if self.rng.random() < C.FAULT_BASE_P + C.FAULT_WEAR_P * st.wear ** 4:
                    self._raise_fault(st)
                    return
                st.unit = self._take_inputs(st)
                st.progress = 0.0
            st.progress += dt
            st.status = "Working"
            if st.progress >= st.cycle_s:
                self._finish(st)

    def _push(self, st, unit):
        if len(self.bins[st.output]) >= C.BIN_CAPACITY:
            return False
        self.bins[st.output].append(unit)
        return True

    def _batch(self, kind):
        self._ids[kind] += 1
        n = (self._ids[kind] - 1) // C.BATCH_SIZE[kind] + 1
        return f"{C.BATCH_PREFIX[kind]}-{n:04d}"

    def _new_id(self, prefix):
        self._ids[prefix] += 1
        return f"{prefix}-{self._ids[prefix]:06d}"

    def _take_inputs(self, st):
        k = st.key
        if k == "shell":
            return {"id": self._new_id("SH"), "steel_batch": self._batch("steel")}
        if k == "insulator":
            return {"id": self._new_id("IN"), "ceramic_batch": self._batch("ceramic")}
        if k == "center":
            return {"id": self._new_id("CE"), "ce_coil": self._batch("ce_wire")}
        if k == "ground":
            shell = self.bins["shell"].popleft()
            shell["ge_coil"] = self._batch("ge_wire")
            return shell
        if k == "assembly":
            self._serial += 1
            plug = {"serial": f"SP{self._serial:06d}",
                    "shell": self.bins["shell_ge"].popleft(),
                    "insulator": self.bins["insulator"].popleft(),
                    "center": self.bins["center"].popleft()}
            plug["gap_preset_mm"] = plug["shell"]["gap_preset_mm"]
            self.oee.assembled += 1
            self.trace.plug_assembled(plug, self.order, self.t)
            return plug
        if k == "final_qc":
            return self.bins["assembled"].popleft()
        return self.bins["tested"].popleft()           # packaging

    def _finish(self, st):
        unit, st.unit, st.progress = st.unit, None, 0.0
        st.cycles += 1
        st.wear_cycles += 1
        reason = CHECKS[st.key](self.rng, unit, st.wear)

        if st.key == "final_qc":
            self._events.append({"type": "gap", "t": self.t, "serial": unit["serial"],
                                 "gap_mm": unit["gap_measured_mm"],
                                 "result": "fail" if reason else "pass"})
        if reason:
            self._reject(st, unit, reason)
            return
        st.good += 1
        if st.key == "packaging":
            self._ship(unit)
        else:
            st.held = unit
            if self._push(st, unit):
                st.held = None

    def _reject(self, st, unit, reason):
        st.rejected += 1
        st.scrap_total += st.scrap_eur
        self.order.add_scrap(st.key, st.scrap_eur)
        self.oee.rejected += 1
        self.reject_counts[(st.key, reason)] += 1
        item = unit.get("serial") or unit["id"]
        detail = REASONS[reason][1]
        measured = unit.get({"shell": "thread_mm", "center": "electrode_mm", "ground": "gap_preset_mm",
                             "final_qc": "gap_measured_mm"}.get(st.key, ""))
        self.trace.reject(self.t, st.key, reason, item, self.order.order_id, st.scrap_eur, detail)
        if "serial" in unit:
            self.trace.plug_finished(unit, "rejected", self.t, reason, st.key)
        self._events.append({"type": "reject", "t": self.t, "station": st.key, "reason": reason,
                             "item": item, "scrap_eur": st.scrap_eur, "value": measured})
        self._note("reject", f"{item} rejected at {st.name}: {reason}"
                             + (f" ({measured} mm)" if measured else ""))

    def _ship(self, plug):
        o = self.order
        o.shipped += 1
        self.oee.good += 1
        self.trace.plug_finished(plug, "shipped", self.t)
        if o.done:
            o.t_done = self.t
            self.trace.order_done(o, self.t)
            self.book.done.append(o)
            self._events.append({"type": "order", "t": self.t, "order": o})
            self._note("info", f"Order {o.order_id} ({o.lot_id}) complete: {o.shipped} plugs, "
                               f"scrap {o.scrap_eur:.2f} EUR, margin {o.margin_eur:.2f} EUR "
                               f"({o.margin_pct:.1f} %)")
            self.order = self.book.next_order(self.t)
            self._note("info", f"Next order {self.order.order_id}: {self.order.qty} plugs "
                               f"for {self.order.customer}")
            self._start_maintenance(reason="order changeover")

    # ======================================================================
    # faults and corrective repair
    # ======================================================================

    def _raise_fault(self, st):
        self.state = FAULTED
        st.status = "Fault"
        wo = self.cmms.corrective(st.key, st.fault_code, st.wear, self.t)
        self.trace.work_order(wo)
        self._repair_wo = wo
        self.fault = {"t": self.t, "station": st.key, "station_name": st.name,
                      "code": st.fault_code, "text": FAULT_TEXT[st.fault_code],
                      "wo": wo.wo_id, "acknowledged": False}
        self.alarms.append(self.fault)
        self._events.append({"type": "alarm", "t": self.t, "station": st.key,
                             "code": st.fault_code, "wo": wo})
        self._note("fault", f"FAULT at {st.name}: {st.fault_code}, {FAULT_TEXT[st.fault_code]} "
                            f"(tool wear {100 * st.wear:.0f} %). {wo.wo_id} opened.")
        if self.auto_operator:
            self._reset_at = self.t + self.rng.uniform(*C.OPERATOR_REACTION_S)

    def _repair_step(self, dt):
        self._repair_left -= dt
        if self._repair_left > 0:
            return
        wo = self._repair_wo
        st = self.stations[wo.station]
        st.wear_cycles = 0                       # the repair fits a new tool
        wo.t_done = self.t
        self.trace.work_order(wo)
        for other in self.cmms.open_for(st.key):  # a planned service is now pointless
            other.t_done = self.t
            other.reason += " (closed by repair)"
            self.trace.work_order(other)
        self._events.append({"type": "workorder", "t": self.t, "wo": wo})
        self._note("info", f"{wo.wo_id} done: {st.name} repaired, new tool fitted")
        self.fault = None
        self._repair_wo = None
        self.state = STOPPED
        if self.auto_operator:
            self.start()

    # ======================================================================
    # predictive maintenance
    # ======================================================================

    def _predictive_check(self):
        opened, urgent = self.cmms.check_wear(self.stations, self.t)
        for wo in opened:
            self.trace.work_order(wo)
            self._note("warn", f"{wo.wo_id} planned: {self.stations[wo.station].name} "
                               f"tool at {wo.wear_pct:.0f} % of life, service at next order change")
        if urgent:
            self._start_maintenance(reason="tool at end of life")

    def _start_maintenance(self, reason):
        queue = [w for w in self.cmms.open_orders if w.kind == PREDICTIVE and w.t_start is None]
        if not queue:
            return
        self._maint_queue = queue
        self.state = MAINTENANCE
        total = sum(w.duration_s for w in queue)
        self._note("warn", f"Scheduled maintenance ({reason}): {len(queue)} work order(s), "
                           f"{total / 60:.0f} min")
        self._next_maintenance()

    def _next_maintenance(self):
        self._maint_wo = self._maint_queue.pop(0)
        self._maint_wo.t_start = self.t
        self._maint_left = self._maint_wo.duration_s

    def _maintenance_step(self, dt):
        self._maint_left -= dt
        if self._maint_left > 0:
            return
        wo = self._maint_wo
        st = self.stations[wo.station]
        st.wear_cycles = 0
        wo.t_done = self.t
        self.trace.work_order(wo)
        self._events.append({"type": "workorder", "t": self.t, "wo": wo})
        self._note("info", f"{wo.wo_id} done: {st.name} serviced before failure")
        if self._maint_queue:
            self._next_maintenance()
        else:
            self._maint_wo = None
            self.state = RUNNING

    # ======================================================================
    # signals and helpers
    # ======================================================================

    def _temperatures(self, dt):
        st = self.stations["ground"]
        target = C.WELD_HOT_C if st.status == "Working" else C.AMBIENT_C
        st.temperature += (target - st.temperature) * dt / C.TEMP_TAU_S
        st.temperature += self.rng.gauss(0, 0.05)

    def inject_fault(self, station_key):
        """Demo button in the HMI: break a station on purpose to show the alarm handling."""
        if self.state != RUNNING:
            return False, "Faults can only be injected while the line runs"
        self._raise_fault(self.stations[station_key])
        return True, f"Fault injected at {self.stations[station_key].name}"

    def _note(self, level, text):
        self.log_seq += 1
        self.log.append((self.t, level, text))

    def drain_events(self):
        ev, self._events = self._events, []
        return ev

    @property
    def scrap_total(self):
        return sum(st.scrap_total for st in self.stations.values())

    @property
    def shipped_total(self):
        return self.stations["packaging"].good

    @property
    def rejected_total(self):
        return sum(st.rejected for st in self.stations.values())

    @property
    def margin_total(self):
        orders = self.book.done + [self.order]
        return sum(o.margin_eur for o in orders)

    def snapshot(self):
        """A plain copy of everything a front end may show."""
        o = self.order
        return {
            "t": self.t, "clock": hms(self.t), "state": self.state, "e10": self.e10_state(),
            "strategy": self.cmms.strategy,
            "stations": [{
                "key": s.key, "no": s.no, "name": s.name, "status": s.status,
                "progress": s.progress / s.cycle_s, "wear": s.wear, "good": s.good,
                "rejected": s.rejected, "scrap_eur": s.scrap_total, "temperature": s.temperature,
                "output_fill": len(self.bins[s.output]) if s.output else None,
            } for s in self.stations.values()],
            "bins": {b: len(q) for b, q in self.bins.items()},
            "order": {"id": o.order_id, "lot": o.lot_id, "customer": o.customer, "qty": o.qty,
                      "shipped": o.shipped, "price": o.price, "scrap_eur": o.scrap_eur,
                      "margin_eur": o.margin_eur, "progress": o.progress},
            "orders_done": len(self.book.done),
            "shipped": self.shipped_total, "rejected": self.rejected_total,
            "scrap_eur": self.scrap_total, "margin_eur": self.margin_total,
            "oee": {"oee": self.oee.oee, "availability": self.oee.availability,
                    "performance": self.oee.performance, "quality": self.oee.quality},
            "fault": dict(self.fault) if self.fault else None,
            "repair_left": self._repair_left if self.state == REPAIRING else None,
            "maint_left": self._maint_left if self.state == MAINTENANCE else None,
            "maint_station": self._maint_wo.station if self._maint_wo else None,
            "work_orders": [{"id": w.wo_id, "station": w.station, "kind": w.kind,
                             "reason": w.reason, "status": w.status, "wear_pct": w.wear_pct}
                            for w in self.cmms.orders[-12:]],
            "log": list(self.log)[-40:],
        }
