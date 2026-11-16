"""
config.py
=========
Every number that describes the line lives here, so an experiment is a change
in one file and not a hunt through the code. Market values (prices, costs) are
assumptions taken from the product definition (HW5), not real data.
"""

LINE_ID = "SP1"
TICK_S = 1.0                      # one simulation step is one second

# --- product -----------------------------------------------------------------
GAP_TARGET_MM = 0.80              # electrode gap the customer wants
GAP_TOL_MM = 0.05                 # final QC: reject outside 0.75 .. 0.85 mm
GAP_GROSS_MM = 0.15               # station 4: reject gross bending errors

THREAD_NOMINAL_MM = 13.97         # M14 x 1.25 major diameter
THREAD_TOL_MM = 0.05
ELECTRODE_NOMINAL_MM = 19.00
ELECTRODE_MIN_MM = 18.85

# --- stations ------------------------------------------------------------------
# key: (number, display name, cycle time s, tool life in cycles,
#       input bins, output bin, scrap loss EUR if rejected here,
#       predictive service time s, corrective repair time s, fault code)
STATIONS = {
    "shell":      (1, "Shell",            4.2, 2600, [],                                           "shell",     0.35, 300,  900, "TOOL_BREAK"),
    "insulator":  (2, "Insulator",        4.5, 7000, [],                                           "insulator", 0.30, 420, 1200, "KILN_OVERTEMP"),
    "center":     (3, "Center electrode", 4.0, 4200, [],                                           "center",    0.15, 240,  780, "WIRE_FEED_JAM"),
    "ground":     (4, "Ground electrode", 4.6, 1900, ["shell"],                                    "shell_ge",  0.45, 360, 1080, "WELD_TIP_STUCK"),
    "assembly":   (5, "Assembly",         5.0, 5200, ["shell_ge", "insulator", "center"],          "assembled", 1.05, 480, 1500, "PRESS_OVERLOAD"),
    "final_qc":   (6, "Final QC",         4.4, 9000, ["assembled"],                                "tested",    1.10, 180,  600, "GAUGE_DRIFT"),
    "packaging":  (7, "Packaging",        3.8, 9999, ["tested"],                                   None,        0.00, 120,  480, "LABEL_JAM"),
}
STATION_ORDER = ["shell", "insulator", "center", "ground", "assembly", "final_qc", "packaging"]
BOTTLENECK = "assembly"           # sets the ideal cycle time of the line
BIN_CAPACITY = 12

# --- money (EUR) ---------------------------------------------------------------
COST_SHIPPED_PLUG = 1.20          # material, work and packaging of a good plug

# --- maintenance -----------------------------------------------------------------
PLAN_AT_WEAR = 0.40               # predictive: open a work order at 40 % of tool life
                                  # (chosen with: python experiment.py --sweep)
URGENT_AT_WEAR = 1.00             # predictive: service immediately at 100 %
FAULT_BASE_P = 0.00004            # chance of a breakdown per cycle with a new tool
FAULT_WEAR_P = 0.004              # extra chance at end of life, grows with wear^4
OPERATOR_REACTION_S = (60, 240)   # headless operator: time until Reset is pressed

# --- quality, how wear turns into defects ---------------------------------------------
THREAD_SIGMA = 0.012
THREAD_DRIFT_AT_LIFE = 0.035      # mean shift of the thread at 100 % wear
CRACK_P = 0.006
CRACK_WEAR_P = 0.010
ELECTRODE_SIGMA = 0.05
ELECTRODE_DRIFT_AT_LIFE = -0.08
WELD_FAIL_P = 0.004
WELD_FAIL_WEAR_P = 0.040
GAP_SIGMA = 0.020
GAP_SIGMA_WEAR = 0.012            # extra scatter of the gap at 100 % wear of the weld station
GAP_GROSS_P = 0.004               # bending error that station 4 catches
CRIMP_GAP_SIGMA = 0.008           # assembly moves the gap a little
SEAL_LEAK_P = 0.003
SEAL_LEAK_WEAR_P = 0.030
GAUGE_NOISE = 0.003

# --- welding temperature (coupled to the station state) ------------------------------
AMBIENT_C = 24.0
WELD_HOT_C = 68.0
TEMP_TAU_S = 90.0                 # time constant of heating and cooling

# --- material batches for traceability -----------------------------------------------------
BATCH_SIZE = {"steel": 500, "ceramic": 400, "ce_wire": 650, "ge_wire": 800}
BATCH_PREFIX = {"steel": "STL", "ceramic": "CER", "ce_wire": "CEW", "ge_wire": "GEW"}

# --- customers (fictional brands) -------------------------------------------------------------
CUSTOMERS = [
    ("NordDrive Parts", 2.20),
    ("Velora Automotive", 2.35),
    ("Kraftwerk Aftermarket", 2.15),
    ("Brightline Motor Supply", 2.25),
    ("Alpen Ignition", 2.30),
]
ORDER_QTY = (250, 600)
