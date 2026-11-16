"""
experiment.py
=============
Does predictive maintenance pay off on this line? Runs the same shifts (same
random seeds) once with each strategy and compares the averages.

    python experiment.py                  20 shifts of 8 hours per strategy
    python experiment.py --shifts 50 --csv results.csv
"""

import argparse
import csv
import statistics as stats

from sparkplug import PREDICTIVE, REACTIVE, ProductionLine

METRICS = [
    ("availability", "Availability", "%", lambda L: 100 * L.oee.availability),
    ("oee", "OEE", "%", lambda L: 100 * L.oee.oee),
    ("quality", "Quality", "%", lambda L: 100 * L.oee.quality),
    ("unscheduled_h", "Unscheduled downtime", "h", lambda L: L.state_seconds["Unscheduled Downtime"] / 3600),
    ("scheduled_h", "Scheduled downtime", "h", lambda L: L.state_seconds["Scheduled Downtime"] / 3600),
    ("breakdowns", "Breakdowns", "", lambda L: sum(1 for w in L.cmms.orders if w.kind == "corrective")),
    ("shipped", "Plugs shipped", "", lambda L: L.shipped_total),
    ("scrap_eur", "Scrap cost", "EUR", lambda L: L.scrap_total),
    ("margin_eur", "Margin", "EUR", lambda L: L.margin_total),
]


def run(strategy, seed, hours):
    line = ProductionLine(seed=seed, strategy=strategy, auto_operator=True)
    line.start()
    line.run(hours * 3600)
    return {key: f(line) for key, _, _, f in METRICS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shifts", type=int, default=20)
    ap.add_argument("--hours", type=float, default=8)
    ap.add_argument("--csv")
    ap.add_argument("--sweep", action="store_true", help="try several planning thresholds")
    a = ap.parse_args()

    if a.sweep:
        from sparkplug import config as C
        base = {k: stats.mean(r[k] for r in (run(REACTIVE, s, a.hours) for s in range(1, a.shifts + 1)))
                for k in ("availability", "margin_eur")}
        print(f"\nReactive: availability {base['availability']:.1f} %, margin {base['margin_eur']:,.0f} EUR\n")
        print("| Plan service at | Availability | OEE | Breakdowns | Scheduled downtime | Margin |")
        print("|---:|---:|---:|---:|---:|---:|")
        for th in (0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 0.9):
            C.PLAN_AT_WEAR = th
            rows = [run(PREDICTIVE, s, a.hours) for s in range(1, a.shifts + 1)]
            m = {k: stats.mean(r[k] for r in rows) for k in rows[0]}
            print(f"| {100 * th:.0f} % | {m['availability']:.1f} % | {m['oee']:.1f} % | {m['breakdowns']:.2f} |"
                  f" {m['scheduled_h']:.2f} h | {m['margin_eur']:,.0f} EUR |")
        return

    results = {s: [run(s, seed, a.hours) for seed in range(1, a.shifts + 1)] for s in (PREDICTIVE, REACTIVE)}

    print(f"\n{a.shifts} shifts of {a.hours:g} h each, same seeds for both strategies\n")
    print(f"| Metric | Predictive | Reactive | Difference |")
    print(f"|---|---:|---:|---:|")
    for key, label, unit, _ in METRICS:
        p = stats.mean(r[key] for r in results[PREDICTIVE])
        r = stats.mean(r[key] for r in results[REACTIVE])
        u = f" {unit}" if unit else ""
        print(f"| {label} | {p:,.2f}{u} | {r:,.2f}{u} | {p - r:+,.2f}{u} |")
    wins = sum(1 for p, r in zip(results[PREDICTIVE], results[REACTIVE]) if p["margin_eur"] > r["margin_eur"])
    print(f"\nPredictive had the higher margin in {wins} of {a.shifts} shifts.")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["strategy", "seed"] + [k for k, *_ in METRICS])
            for s, rows in results.items():
                for seed, row in enumerate(rows, start=1):
                    w.writerow([s, seed] + [round(row[k], 4) for k, *_ in METRICS])
        print(f"Raw results in {a.csv}")


if __name__ == "__main__":
    main()
