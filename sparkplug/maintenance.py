"""
maintenance.py
==============
A small CMMS (computerised maintenance management system).

Two strategies, so they can be compared on the same shift:

predictive  Each station counts the cycles since its last service. At a set share
            of the tool life (config.PLAN_AT_WEAR, 40 %) a PREDICTIVE work order is opened and carried out at
            the next order changeover, as Scheduled Downtime. If a tool reaches
            100 % before the changeover, it is serviced at once.
reactive    No planned service. A tool runs until the station breaks down; a
            CORRECTIVE work order is opened, the line stands in Unscheduled
            Downtime and the repair takes longer than a planned service.

In both cases a breakdown can still happen (a new tool has a small base risk),
and every breakdown produces a corrective work order.
"""

from dataclasses import dataclass

from . import config as C

PREDICTIVE = "predictive"
REACTIVE = "reactive"


@dataclass
class WorkOrder:
    wo_id: str
    station: str
    kind: str            # "predictive" or "corrective"
    reason: str
    wear_pct: float
    t_created: float
    duration_s: float
    t_start: float = None
    t_done: float = None

    @property
    def status(self):
        if self.t_done is not None:
            return "done"
        if self.t_start is not None:
            return "in progress"
        return "open"


class CMMS:
    def __init__(self, strategy=PREDICTIVE):
        self.strategy = strategy
        self.count = 0
        self.orders = []          # every work order ever opened

    def _new(self, station, kind, reason, wear, t, duration):
        self.count += 1
        wo = WorkOrder(f"WO-{self.count:04d}", station, kind, reason, 100 * wear, t, duration)
        self.orders.append(wo)
        return wo

    def open_for(self, station):
        return [w for w in self.orders if w.station == station and w.t_done is None]

    @property
    def open_orders(self):
        return [w for w in self.orders if w.t_done is None]

    def check_wear(self, stations, t):
        """Predictive rule. Returns (newly opened work orders, urgent ones)."""
        if self.strategy != PREDICTIVE:
            return [], []
        opened, urgent = [], []
        for st in stations.values():
            wear = st.wear
            if wear >= C.PLAN_AT_WEAR and not self.open_for(st.key):
                svc = C.STATIONS[st.key][7]
                opened.append(self._new(st.key, PREDICTIVE,
                                        f"Tool at {100 * wear:.0f} % of life", wear, t, svc))
            if wear >= C.URGENT_AT_WEAR:
                urgent += [w for w in self.open_for(st.key) if w.kind == PREDICTIVE]
        return opened, urgent

    def corrective(self, station, fault_code, wear, t):
        repair = C.STATIONS[station][8]
        return self._new(station, "corrective", fault_code, wear, t, repair)
