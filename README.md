# Spark Plug Production Line SP1

Final project for **Advanced Programming** (SRH Hochschule Berlin), Dany Degheady.

A simulated make-to-order line that produces spark plugs for business customers who sell them under their own brand. Seven stations, a Python back end that decides when and why a plug is defective, a Tkinter operator screen, InfluxDB as the time series store and a Grafana dashboard that is set up automatically.

Three layers go beyond the required parts:

- **Money**: every rejected part has a scrap cost, every customer order has a margin, and the line reports a full OEE (availability, performance, quality).
- **Predictive maintenance (CMMS)**: tool wear is counted per station, work orders are planned before a tool fails and carried out at the next order changeover.
- **Lot traceability**: every shipped plug is linked to the material batches it contains, so a supplier problem can be traced to the customers who received the affected plugs.

## The line

```
1 Shell ------------> [bin] --> 4 Ground electrode --> [bin] --+
2 Insulator --------> [bin] -----------------------------------+--> 5 Assembly --> [bin] --> 6 Final QC --> [bin] --> 7 Packaging --> customer order
3 Center electrode -> [bin] -----------------------------------+
```

| # | Station | Check | Reject reason | Scrap cost |
|---|---|---|---|---|
| 1 | Shell | thread diameter 13.97 +/- 0.05 mm | `THREAD_OUT_OF_TOLERANCE` | 0.35 EUR |
| 2 | Insulator | no crack after sintering | `CRACKED_INSULATOR` | 0.30 EUR |
| 3 | Center electrode | length at least 18.85 mm | `ELECTRODE_TOO_SHORT` | 0.15 EUR |
| 4 | Ground electrode | weld holds, gap pre-set within 0.15 mm | `WELD_FAILURE`, `GAP_PRESET_OUT_OF_RANGE` | 0.45 EUR |
| 5 | Assembly | seal tight | `SEAL_LEAK` | 1.05 EUR |
| 6 | Final QC | gap 0.80 +/- 0.05 mm | `GAP_OUT_OF_TOLERANCE` | 1.10 EUR |
| 7 | Packaging | count matches the order | | |

Tool wear raises the defect rate: a worn cutting tool shifts the thread diameter, a worn weld tip scatters the gap and makes weld failures more likely. That is the link between maintenance, quality and money.

## Project structure

```
sparkplug/            the back end (no drawing code, no database client inside the logic)
  config.py           every number: cycle times, tolerances, costs, wear, customers
  line.py             stations, bins, line states (Running, Stopped, Faulted, Repairing, Maintenance)
  quality.py          one check per station, returns the reject reason
  maintenance.py      CMMS: predictive and corrective work orders
  economics.py        customer orders, margin, OEE
  traceability.py     SQLite store for plugs, rejects, orders and work orders; recall query
  telemetry.py        writes the line to InfluxDB (line protocol)
hmi.py                operator screen (Tkinter)
run_headless.py       runs a shift without a window, optionally into InfluxDB
recall.py             traceability queries from the command line
experiment.py         predictive vs reactive maintenance over many shifts
docker/               InfluxDB + Grafana with provisioned data source and dashboard
tests/                23 pytest tests
```

## Run it (Windows PowerShell)

```powershell
pip install -r requirements.txt

# 1. database and dashboard (stop the Grafana lab first if it is still running)
cd docker
docker compose up -d
cd ..

# 2. fill InfluxDB with an 8 hour shift that ends now
python run_headless.py --influx

# 3. open http://localhost:3000  (admin / admin), dashboard "Spark Plug Line SP1"

# 4. operate the line live, every second goes to InfluxDB as well
python hmi.py --influx --speed 10x
```

Without Docker everything except the dashboard still works: `python hmi.py`, `python run_headless.py`.

### Traceability

```powershell
python recall.py --list              # material batches in shipped plugs
python recall.py CER-0004            # which customers received plugs with ceramic batch CER-0004
python recall.py --lot LOT-0003      # which material batches are in a customer lot
python recall.py --serial SP000123   # history of one plug
```

### Experiment: does predictive maintenance pay?

```powershell
python experiment.py           # 20 shifts per strategy, same random seeds
python experiment.py --sweep   # which planning threshold earns the most
```

Result (20 shifts of 8 h, planning threshold 40 % of tool life):

| Metric | Predictive | Reactive |
|---|---:|---:|
| Availability | 92.6 % | 74.3 % |
| OEE | 85.1 % | 64.9 % |
| Breakdowns per shift | 1.9 | 6.6 |
| Plugs shipped | 4,330 | 3,892 |
| Margin per shift | 4,288 EUR | 3,712 EUR |

Predictive maintenance had the higher margin in 20 of 20 shifts. The sweep shows why the threshold is 40 % and not lower: OEE keeps rising when the line is serviced earlier, because planned maintenance does not count against OEE, but the margin falls again below 40 %.

## Tests

```powershell
python -m pytest -q
```

The tests force each reject reason, check the Start/Stop/Reset logic and the fault handling, the predictive work orders, the margin formula, that no plug disappears between assembly and shipping, and that a recall finds the right customers.

## Use of AI

The code was written with Claude (Anthropic) as a pair programmer. Design decisions, the product definition and every result were checked by me; the prompts, the outputs and the corrections are documented in the appendix of the project report.
