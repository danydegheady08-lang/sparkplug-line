"""Tests for the spark plug line back end. Run with:  python -m pytest -q"""

import random

import pytest

from sparkplug import PREDICTIVE, REACTIVE, ProductionLine
from sparkplug import config as C
from sparkplug import quality as Q
from sparkplug.line import (FAULTED, MAINTENANCE, REPAIRING, RUNNING, SCHEDULED, STOPPED,
                            UNSCHEDULED)


def shift(seconds=3600, seed=7, strategy=PREDICTIVE):
    line = ProductionLine(seed=seed, strategy=strategy, auto_operator=True)
    line.start()
    line.run(seconds)
    return line


# ---------------------------------------------------------------------------
# when and why a part is defective
# ---------------------------------------------------------------------------

class FixedRng:
    """A random generator that returns chosen values, to force each check."""

    def __init__(self, random_value=0.5, gauss_offset=0.0):
        self.r, self.g = random_value, gauss_offset

    def random(self):
        return self.r

    def gauss(self, mu, sigma):
        return mu + self.g

    def uniform(self, a, b):
        return b

    def choice(self, seq):
        return seq[0]


def test_thread_out_of_tolerance():
    assert Q.check_shell(FixedRng(gauss_offset=0.06), {}, 0.0) == Q.THREAD_OUT_OF_TOLERANCE
    assert Q.check_shell(FixedRng(gauss_offset=0.0), {}, 0.0) is None


def test_worn_tool_shifts_thread_and_fails():
    # at 150 % wear the mean has drifted by 1.5 x 0.035 mm, which exceeds the tolerance
    assert Q.check_shell(FixedRng(), {}, 1.5) == Q.THREAD_OUT_OF_TOLERANCE


def test_cracked_insulator():
    assert Q.check_insulator(FixedRng(random_value=0.0), {}, 0.0) == Q.CRACKED_INSULATOR
    assert Q.check_insulator(FixedRng(random_value=0.99), {}, 0.0) is None


def test_electrode_too_short():
    assert Q.check_center(FixedRng(gauss_offset=-0.2), {}, 0.0) == Q.ELECTRODE_TOO_SHORT


def test_weld_failure_and_gap_preset():
    assert Q.check_ground(FixedRng(random_value=0.0), {}, 0.0) == Q.WELD_FAILURE
    unit = {}
    assert Q.check_ground(FixedRng(random_value=0.99, gauss_offset=0.2), unit, 0.0) \
        == Q.GAP_PRESET_OUT_OF_RANGE
    assert unit["gap_preset_mm"] == pytest.approx(1.0)


def test_seal_leak():
    unit = {"gap_preset_mm": 0.80}
    assert Q.check_assembly(FixedRng(random_value=0.0), unit, 0.0) == Q.SEAL_LEAK


def test_final_qc_gap_limits():
    assert Q.check_final_qc(FixedRng(), {"gap_mm": 0.86}, 0.0) == Q.GAP_OUT_OF_TOLERANCE
    assert Q.check_final_qc(FixedRng(), {"gap_mm": 0.74}, 0.0) == Q.GAP_OUT_OF_TOLERANCE
    assert Q.check_final_qc(FixedRng(), {"gap_mm": 0.84}, 0.0) is None


def test_every_reject_is_recorded_with_station_and_reason():
    line = shift(3600)
    rows = line.trace.db.execute("SELECT station, reason, item_id, scrap_eur FROM rejects").fetchall()
    assert len(rows) == line.rejected_total > 0
    for station, reason, item, scrap in rows:
        assert Q.REASONS[reason][0] == station          # the reason belongs to that station
        assert item and scrap == C.STATIONS[station][6]


# ---------------------------------------------------------------------------
# operator commands and line states
# ---------------------------------------------------------------------------

def test_start_stop():
    line = ProductionLine()
    assert line.state == STOPPED
    assert line.start()[0] and line.state == RUNNING
    line.run(60)
    assert line.stop()[0] and line.state == STOPPED
    t_parts = line.stations["shell"].cycles
    line.run(60)
    assert line.stations["shell"].cycles == t_parts     # nothing moves while stopped


def test_fault_needs_reset_and_repair(monkeypatch):
    monkeypatch.setattr(C, "FAULT_BASE_P", 1.0)          # break down at the first cycle
    line = ProductionLine()
    line.start()
    line.tick()
    assert line.state == FAULTED
    assert line.e10_state() == UNSCHEDULED
    assert line.fault["code"] == C.STATIONS[line.fault["station"]][9]
    ok, msg = line.start()
    assert not ok and "RESET" in msg                     # cannot restart a faulted line
    monkeypatch.setattr(C, "FAULT_BASE_P", 0.0)
    assert line.reset()[0] and line.state == REPAIRING
    line.run(C.STATIONS[line.fault["station"]][8] + 1)
    assert line.state == STOPPED and line.fault is None
    assert line.cmms.orders[0].kind == "corrective" and line.cmms.orders[0].status == "done"


# ---------------------------------------------------------------------------
# predictive maintenance
# ---------------------------------------------------------------------------

def test_predictive_work_order_waits_for_order_change(monkeypatch):
    monkeypatch.setattr(C, "FAULT_BASE_P", 0.0)
    monkeypatch.setattr(C, "FAULT_WEAR_P", 0.0)
    line = ProductionLine(strategy=PREDICTIVE)
    st = line.stations["ground"]
    st.wear_cycles = int(0.8 * st.life)
    line.start()
    line.tick()
    wo = line.cmms.open_for("ground")[0]
    assert wo.kind == "predictive" and wo.status == "open"
    assert line.state == RUNNING                         # no stop yet
    line.order.qty = line.order.shipped + 1               # finish the order soon
    while line.state != MAINTENANCE:
        line.tick()
    assert line.e10_state() == SCHEDULED
    line.run(wo.duration_s + 1)
    assert wo.status == "done" and st.wear_cycles < 5 and line.state == RUNNING


def test_urgent_service_at_end_of_life(monkeypatch):
    monkeypatch.setattr(C, "FAULT_BASE_P", 0.0)
    monkeypatch.setattr(C, "FAULT_WEAR_P", 0.0)
    line = ProductionLine(strategy=PREDICTIVE)
    line.stations["shell"].wear_cycles = line.stations["shell"].life
    line.start()
    line.tick()
    assert line.state == MAINTENANCE


def test_reactive_never_plans():
    line = shift(8 * 3600, strategy=REACTIVE)
    assert all(w.kind == "corrective" for w in line.cmms.orders)


def test_predictive_beats_reactive_on_availability():
    # averaged over seeds, so the test does not hang on one lucky shift
    def avail(strategy):
        return sum(shift(8 * 3600, seed=s, strategy=strategy).oee.availability
                   for s in range(1, 7)) / 6
    assert avail(PREDICTIVE) > avail(REACTIVE)


# ---------------------------------------------------------------------------
# money, OEE and conservation of parts
# ---------------------------------------------------------------------------

def test_order_margin_formula():
    line = shift(4 * 3600)
    o = line.book.done[0]
    expected = o.price * o.shipped - (C.COST_SHIPPED_PLUG * o.shipped + o.scrap_eur)
    assert o.margin_eur == pytest.approx(expected)
    assert o.shipped == o.qty


def test_scrap_adds_up():
    line = shift(4 * 3600)
    by_order = sum(o.scrap_eur for o in line.book.done) + line.order.scrap_eur
    assert by_order == pytest.approx(line.scrap_total)


def test_oee_is_product_of_its_parts():
    line = shift(4 * 3600)
    o = line.oee
    for v in (o.availability, o.performance, o.quality, o.oee):
        assert 0.0 <= v <= 1.0
    assert o.oee == pytest.approx(o.availability * o.performance * o.quality)


def test_no_plug_disappears():
    line = shift(4 * 3600)
    st = line.stations
    wip = len(line.bins["assembled"]) + len(line.bins["tested"])
    wip += sum(1 for k in ("assembly", "final_qc", "packaging")
               for u in (st[k].unit, st[k].held) if u is not None)
    plug_rejects = st["assembly"].rejected + st["final_qc"].rejected
    assert line.oee.assembled == line.shipped_total + plug_rejects + wip


def test_same_seed_same_shift():
    a, b = shift(2 * 3600, seed=3), shift(2 * 3600, seed=3)
    assert a.shipped_total == b.shipped_total and a.scrap_total == b.scrap_total


def test_weld_temperature_follows_state():
    line = ProductionLine()
    line.start()
    line.run(600)
    hot = line.stations["ground"].temperature
    line.stop()
    line.run(600)
    cold = line.stations["ground"].temperature
    assert hot > 55 and cold < hot - 20


# ---------------------------------------------------------------------------
# traceability
# ---------------------------------------------------------------------------

def test_every_shipped_plug_is_traceable():
    line = shift(4 * 3600)
    n = line.trace.db.execute("SELECT COUNT(*) FROM plugs WHERE status='shipped'").fetchone()[0]
    assert n == line.shipped_total


def test_recall_finds_the_customer():
    line = shift(4 * 3600)
    serial, batch, order_id, customer = line.trace.db.execute(
        "SELECT serial, ceramic_batch, order_id, customer FROM plugs WHERE status='shipped' LIMIT 1"
    ).fetchone()
    hits = line.trace.recall(batch)
    assert any(h["order_id"] == order_id and h["customer"] == customer for h in hits)
    total = line.trace.db.execute(
        "SELECT COUNT(*) FROM plugs WHERE ceramic_batch=? AND status='shipped'", (batch,)).fetchone()[0]
    assert sum(h["plugs"] for h in hits) == total
    assert total <= C.BATCH_SIZE["ceramic"]


def test_recall_rejects_unknown_batch_type():
    with pytest.raises(ValueError):
        ProductionLine().trace.recall("XYZ-0001")
