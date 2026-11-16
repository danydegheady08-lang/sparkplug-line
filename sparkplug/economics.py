"""
economics.py
============
The money layer: customer orders, scrap cost, margin per order and OEE.

margin = price x shipped - (cost of shipped plugs + scrap cost of the order)

A reject is charged to the order that is running when it happens. A part made
at station 1 in the last seconds of order A may end up in order B, so the split
between two neighbouring orders is approximate; the total over a shift is exact.
"""

from dataclasses import dataclass, field

from . import config as C


@dataclass
class Order:
    order_id: str
    lot_id: str
    customer: str
    qty: int
    price: float
    t_start: float
    shipped: int = 0
    scrap_eur: float = 0.0
    scrap_by_station: dict = field(default_factory=dict)
    t_done: float = None

    @property
    def done(self):
        return self.shipped >= self.qty

    @property
    def revenue_eur(self):
        return self.shipped * self.price

    @property
    def margin_eur(self):
        return self.revenue_eur - self.shipped * C.COST_SHIPPED_PLUG - self.scrap_eur

    @property
    def margin_pct(self):
        return 100.0 * self.margin_eur / self.revenue_eur if self.revenue_eur else 0.0

    @property
    def progress(self):
        return self.shipped / self.qty

    def add_scrap(self, station, eur):
        self.scrap_eur += eur
        self.scrap_by_station[station] = self.scrap_by_station.get(station, 0.0) + eur


class OrderBook:
    """Creates the next order when one is finished (the simulation's ERP)."""

    def __init__(self, rng):
        self.rng = rng
        self.count = 0
        self.done = []

    def next_order(self, t):
        self.count += 1
        customer, price = self.rng.choice(C.CUSTOMERS)
        qty = self.rng.randrange(C.ORDER_QTY[0], C.ORDER_QTY[1] + 1, 50)
        return Order(order_id=f"PO-{1000 + self.count}", lot_id=f"LOT-{self.count:04d}",
                     customer=customer, qty=qty, price=price, t_start=t)


class OEE:
    """Availability x Performance x Quality, counted second by second.

    planned time  = seconds the line was meant to produce: running, or stopped
                    by a fault or its repair. Operator stops and scheduled
                    maintenance are not planned production time.
    run time      = planned time minus unscheduled downtime
    performance   = ideal cycle time x plugs assembled / run time
    quality       = good plugs / (good plugs + all rejected parts and plugs)
    """

    def __init__(self, ideal_cycle_s):
        self.ideal = ideal_cycle_s
        self.planned_s = 0.0
        self.run_s = 0.0
        self.assembled = 0
        self.good = 0
        self.rejected = 0

    def tick(self, dt, planned, running):
        if planned:
            self.planned_s += dt
        if running:
            self.run_s += dt

    @property
    def availability(self):
        return self.run_s / self.planned_s if self.planned_s else 0.0

    @property
    def performance(self):
        return min(1.0, self.ideal * self.assembled / self.run_s) if self.run_s else 0.0

    @property
    def quality(self):
        total = self.good + self.rejected
        return self.good / total if total else 1.0

    @property
    def oee(self):
        return self.availability * self.performance * self.quality
