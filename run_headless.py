"""
run_headless.py
===============
Run the spark plug line without a window and print a shift report.

    python run_headless.py                         8 hour shift, report only
    python run_headless.py --influx                ... and fill InfluxDB with it
    python run_headless.py --hours 2 --strategy reactive --seed 3
    python run_headless.py --dry-run points.lp     write line protocol to a file
    python run_headless.py --influx --realtime --speed 10   live feed for Grafana

By default the shift is written with timestamps that END NOW, so a Grafana
dashboard set to "Last 12 hours" shows it straight away. With --realtime the
simulation runs against the wall clock instead (speed x faster).
"""

import argparse
import time

from sparkplug import PREDICTIVE, REACTIVE, ProductionLine
from sparkplug import config as C
from sparkplug.line import hms
from sparkplug.telemetry import FileSink, InfluxSink, Telemetry


def report(line):
    s = line.snapshot()
    hours = line.t / 3600
    print(f"\nShift report  ({hours:.1f} h simulated, strategy: {line.cmms.strategy}, line {C.LINE_ID})")
    print("=" * 72)
    for state, sec in sorted(line.state_seconds.items(), key=lambda kv: -kv[1]):
        print(f"  {state:<22} {sec / 3600:6.2f} h  {100 * sec / line.t:5.1f} %")
    o = s["oee"]
    print(f"\n  OEE {100 * o['oee']:.1f} %  =  availability {100 * o['availability']:.1f} %"
          f"  x performance {100 * o['performance']:.1f} %  x quality {100 * o['quality']:.1f} %")
    print(f"  Plugs shipped {s['shipped']},  parts rejected {s['rejected']},"
          f"  scrap {s['scrap_eur']:.2f} EUR,  margin {s['margin_eur']:.2f} EUR")

    print("\n  Rejects by station and reason")
    for (station, reason), n in line.reject_counts.most_common():
        eur = n * C.STATIONS[station][6]
        print(f"    {C.STATIONS[station][1]:<18} {reason:<26} {n:5d}   {eur:8.2f} EUR")

    print("\n  Customer orders")
    for od in line.book.done + [line.order]:
        tag = "done" if od.done else f"running {100 * od.progress:.0f} %"
        print(f"    {od.order_id} {od.lot_id} {od.customer:<24} {od.shipped:4d}/{od.qty:<4d}"
              f" scrap {od.scrap_eur:7.2f}  margin {od.margin_eur:8.2f} EUR ({od.margin_pct:4.1f} %)  {tag}")

    print("\n  Work orders")
    for w in line.cmms.orders:
        print(f"    {w.wo_id} {C.STATIONS[w.station][1]:<18} {w.kind:<11} {w.status:<12}"
              f" opened {hms(w.t_created)}  {w.reason}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--strategy", choices=[PREDICTIVE, REACTIVE], default=PREDICTIVE)
    ap.add_argument("--trace", default="sparkplug_trace.db", help="SQLite file for traceability")
    ap.add_argument("--influx", action="store_true", help="write to InfluxDB")
    ap.add_argument("--dry-run", metavar="FILE", help="write line protocol to FILE")
    ap.add_argument("--realtime", action="store_true", help="run against the wall clock")
    ap.add_argument("--speed", type=float, default=1.0, help="with --realtime: x times faster")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    line = ProductionLine(seed=args.seed, strategy=args.strategy, trace_path=args.trace,
                          auto_operator=True)
    # a fresh trace file per run, so recalls only see this shift
    for table in ("plugs", "rejects", "orders", "work_orders"):
        line.trace.db.execute(f"DELETE FROM {table}")

    sink = None
    if args.influx:
        sink = InfluxSink()
    elif args.dry_run:
        sink = FileSink(args.dry_run)
    tel = Telemetry(sink) if sink else None

    seconds = int(args.hours * 3600)
    line.start()
    if args.realtime:
        step = 1.0 / args.speed
        print(f"Running live at {args.speed:g}x, Ctrl+C to stop")
        try:
            for _ in range(seconds):
                t_wall = time.time()
                line.tick()
                if tel:
                    tel.record(line, time.time_ns())
                    if isinstance(sink, InfluxSink):
                        sink.flush()
                time.sleep(max(0.0, step - (time.time() - t_wall)))
        except KeyboardInterrupt:
            pass
    else:
        t0_ns = time.time_ns() - seconds * 1_000_000_000
        for _ in range(seconds):
            line.tick()
            if tel:
                tel.record(line, t0_ns + int(line.t * 1e9))
    if tel:
        tel.close()
        print(f"{sink.count} points written")
    line.trace.commit()
    if not args.quiet:
        report(line)
    print(f"Traceability data in {args.trace}  (try: python recall.py --list)")


if __name__ == "__main__":
    main()
